"""
올리브영 리뷰 크롤러 - Streamlit Cloud 배포용
전략: Playwright 브라우저 인터셉트 → curl_cffi API 재현
"""

import re
import json
import time
import random
import subprocess
import html as html_lib
from urllib.parse import urlparse, parse_qs, urlencode

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

    # HTML에서 CDN 번들 추출
    cdn_matches = re.findall(
        r'https://cf-static\.oliveyoung\.co\.kr/[^\s"\'<>]+?\.js',
        html_text,
    )
    bundle_urls.extend(cdn_matches)
    for m in re.findall(r'"(/_next/static/[^"]+?\.js)"', html_text):
        bundle_urls.append(f"https://www.oliveyoung.co.kr{m}")

    # ★ RSC 스트림에서 직접 청크 경로 추출
    # 형식: I[moduleId,["chunkId","static/chunks/xxx.js"],"ExportName"]
    cdn_base = "https://cf-static.oliveyoung.co.kr"
    for chunk_path in re.findall(r'"(static/chunks/[^"]+\.js)"', rsc_text):
        for base in [cdn_base, "https://www.oliveyoung.co.kr"]:
            full = f"{base}/{chunk_path}"
            if full not in bundle_urls:
                bundle_urls.append(full)
    # RSC의 전체 CDN URL도 추출
    for cdn_url in re.findall(r'https://cf-static\.oliveyoung\.co\.kr/[^\s"\'\\]+?\.js', rsc_text):
        if cdn_url not in bundle_urls:
            bundle_urls.append(cdn_url)

    # buildId 및 CDN 프리픽스 추출 후 buildManifest 로드
    # 예: cf-static.oliveyoung.co.kr/lavender/2026032601/_next/static/chunks/xxx.js
    combined = rsc_text + html_text
    cdn_prefix = ""
    cdn_prefix_m = re.search(
        r'(https://cf-static\.oliveyoung\.co\.kr/[a-zA-Z0-9_/-]+?)/_next/static/',
        combined
    )
    if cdn_prefix_m:
        cdn_prefix = cdn_prefix_m.group(1)  # e.g. https://cf-static.../lavender/2026032601

    build_id_m = re.search(r'/_next/static/([a-zA-Z0-9_-]{10,50})/', combined)
    if not build_id_m:
        build_id_m = re.search(r'"buildId"\s*:\s*"([^"]+)"', combined)
    if build_id_m:
        build_id = build_id_m.group(1)
        manifest_candidates = [
            f"https://www.oliveyoung.co.kr/_next/static/{build_id}/_buildManifest.js",
        ]
        if cdn_prefix:
            manifest_candidates.insert(0,
                f"{cdn_prefix}/_next/static/{build_id}/_buildManifest.js")
        for manifest_url in manifest_candidates:
            try:
                mresp = session.get(manifest_url, headers={"User-Agent": PC_UA}, timeout=10)
                if mresp.status_code == 200 and len(mresp.text) > 100:
                    before = len(bundle_urls)
                    for chunk in re.findall(r'"(/_next/static/chunks/[^"]+\.js)"', mresp.text):
                        for base in [cdn_base, "https://www.oliveyoung.co.kr"]:
                            full = f"{base}{chunk}"
                            if full not in bundle_urls:
                                bundle_urls.append(full)
                    added = len(bundle_urls) - before
                    if log:
                        log(f"ℹ️ buildManifest ({build_id[:8]}): +{added}개 청크 추가")
                    break
            except Exception:
                pass

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

    for url in ordered[:20]:
        try:
            resp = session.get(url, headers={"User-Agent": PC_UA, "Accept": "*/*"}, timeout=15)
            if resp.status_code != 200:
                continue
            bundle_text = resp.text
            if log and len(bundle_text) < 500:
                log(f"📦 소형번들[{len(bundle_text)}]: {html_lib.escape(bundle_text[:80])}")

            for p in url_patterns:
                matches = p.findall(bundle_text)
                for m in matches:
                    if 'review' in m.lower() or 'gdas' in m.lower():
                        full_url = m if m.startswith('http') else f"https://www.oliveyoung.co.kr{m}"
                        if log:
                            log(f"🔑 JS 번들 API 발견: {m}")
                        return full_url

            # 번들에서 review/gdas 관련 문자열 찾아 디버그 출력
            fname = url.split('/')[-1][:30]
            found_kw = False
            for kw in ['review', 'gdas', 'Review', 'GDAS']:
                idx = bundle_text.find(f'"{kw}')
                if idx < 0:
                    idx = bundle_text.find(f'/{kw}')
                if idx >= 0:
                    found_kw = True
                    if log:
                        ctx = bundle_text[max(0, idx - 20):idx + 80].replace('\n', ' ')
                        log(f"📦 [{fname}] {html_lib.escape(ctx[:90])}")

                    # ★ 이 번들이 리뷰 코드 포함 → 리뷰 탭 주변 500자 덤프
                    ctx_wide = bundle_text[max(0, idx - 50):idx + 450].replace('\n', ' ')
                    if log:
                        log(f"🔍 [{fname} 주변]: {html_lib.escape(ctx_wide[:400])}")

                    # 동적 임포트 청크 ID 탐색 (webpack: n.e(ID) or __webpack_require__.e(ID))
                    chunk_ids = re.findall(r'\.e\((\d+)\)', bundle_text[max(0, idx-2000):idx+2000])
                    if log and chunk_ids:
                        log(f"🔍 동적청크 ID 후보: {chunk_ids[:10]}")

                    # webpack 청크맵: {숫자:"해시"} 패턴 탐색
                    chunk_map_m = re.search(
                        r'\{(?:\d+:"[a-f0-9]+",?\s*){3,}\}',
                        bundle_text[max(0, idx-5000):idx+5000]
                    )
                    if chunk_map_m and log:
                        log(f"🔍 청크맵: {html_lib.escape(chunk_map_m.group()[:200])}")

                    # oliveyoung 도메인 포함 URL 탐색
                    oy_urls = re.findall(
                        r'["\`]((?:https?:)?//[^\s"\'`\\]{5,120}oliveyoung[^\s"\'`\\]{0,80})["\`]',
                        bundle_text
                    )
                    for u in oy_urls[:5]:
                        if log:
                            log(f"🔑 oliveyoung URL: {html_lib.escape(u[:120])}")

                    # ★ api.dp.oliveyoung.co.kr 주변 맥락 탐색
                    dp_idx = bundle_text.find('api.dp.oliveyoung')
                    if dp_idx >= 0:
                        dp_ctx = bundle_text[max(0, dp_idx - 100):dp_idx + 300].replace('\n', ' ')
                        if log:
                            log(f"🔑 api.dp 맥락: {html_lib.escape(dp_ctx[:350])}")

                    # /review/ 또는 /api/ 경로 탐색
                    api_paths = re.findall(
                        r'["\`](/(?:review|api)/[^\s"\'`\\]{3,80})["\`]',
                        bundle_text
                    )
                    for p in api_paths[:5]:
                        if log:
                            log(f"🔑 API 경로 후보: {html_lib.escape(p[:100])}")
                    break

            if log and not found_kw and ordered.index(url) == 0:
                log(f"📦 첫번들[{len(bundle_text)}chars]: {html_lib.escape(bundle_text[:60])}")
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

    # 정렬 코드 변환 (원본 크롤러 형식)
    sort_map_orig = {"date": None, "useful": "USEFUL_SCORE_DESC",
                     "star_desc": "RATING_DESC", "star_asc": "RATING_ASC"}
    sort_orig = sort_map_orig.get(sort_code)

    # JS 번들에서 발견된 X-Access-Token (api.dp 로깅 API용이지만 v2 인증에도 사용 가능)
    _X_TOKEN = "94A0D5853594D098111EA0C7E5C9CDD09958C842"
    _token_h = {"X-Access-Token": _X_TOKEN}

    # (endpoint, method, origin, body, extra_headers)
    attempts = [
        # ★ v2 path GET + X-Access-Token (BAD_REQUEST 해결 시도)
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"page": page, "size": PAGE_SIZE, "reviewType": "ALL"}, _token_h),
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"pageIdx": page, "rowsPerPage": PAGE_SIZE, "sortCode": "NEW"}, _token_h),
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"page": page, "size": PAGE_SIZE, "channelCd": "MOBILE"}, _token_h),
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"page": page, "size": PAGE_SIZE}, _token_h),
        # ★ v2 path + goodsNo 중복 전달
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"goodsNo": goods_no, "page": page, "size": PAGE_SIZE, "reviewType": "ALL"}, _token_h),
        # ★ v2 path (토큰 없이, 기존과 같음)
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"page": page, "size": PAGE_SIZE, "reviewType": "ALL"}, None),
        # ★ v2 POST (Cloudflare 통과 시도)
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         "https://www.oliveyoung.co.kr",
         {"goodsNumber": goods_no, "page": page, "size": PAGE_SIZE, "reviewType": "ALL"}, _token_h),
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         None,
         {"goodsNumber": goods_no, "page": page, "size": PAGE_SIZE, "reviewType": "ALL"}, _token_h),
        # Next.js API 라우트 가능성
        (f"https://www.oliveyoung.co.kr/api/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"page": page, "size": PAGE_SIZE}, None),
        # v1
        ("https://m.oliveyoung.co.kr/review/api/v1/reviews", "POST",
         "https://m.oliveyoung.co.kr",
         {"goodsNumber": goods_no, "page": page, "size": PAGE_SIZE, "reviewType": "ALL"}, None),
    ]

    for api_url, method, origin, body, extra_h in attempts:
        h = {
            "User-Agent": MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": mobile_product_url,
            "Content-Type": "application/json",
        }
        if origin:
            h["Origin"] = origin
        if extra_h:
            h.update(extra_h)

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
                    # 에러 응답 건너뜀
                    api_status = data.get("status") if isinstance(data, dict) else None
                    if api_status in ("NOT_FOUND", "ERROR", "FAIL", "BAD_REQUEST", "METHOD_NOT_ALLOWED"):
                        if log and page == 1:
                            log(f"ℹ️ {api_status} [{label}]: {data.get('message','')[:80]}")
                        continue
                    reviews: list = []
                    _extract_from_json_structure(data, reviews)
                    # 원본 API 응답 구조: status=SUCCESS, data=[...], totalCnt=N
                    if not reviews and isinstance(data, dict) and api_status == "SUCCESS":
                        review_list = data.get("data") or data.get("list") or []
                        if isinstance(review_list, list):
                            for item in review_list:
                                r = _parse_review_dict(item)
                                if r:
                                    reviews.append(r)
                    total = 0
                    if isinstance(data, dict):
                        for k in ["totalCnt", "totalCount", "total", "pageData"]:
                            if data.get(k) is not None:
                                try:
                                    total = int(data[k])
                                    break
                                except Exception:
                                    pass
                        if log and page == 1 and not reviews:
                            log(f"ℹ️ status={api_status} 키: {list(data.keys())[:8]} | data={str(data.get('data'))[:80]}")
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


# ──────────── Playwright 브라우저 인터셉트 ────────────

def _ensure_playwright_browser(log=None) -> bool:
    """Playwright Chromium 브라우저가 설치돼 있는지 확인하고 없으면 설치."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        if log:
            log("ℹ️ playwright 패키지 미설치 — pip install playwright 필요")
        return False

    import os
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
    already = os.path.isdir(cache_dir) and bool(os.listdir(cache_dir))
    if log:
        log(f"ℹ️ Playwright 캐시: {cache_dir} (존재={already})")
    if already:
        return True

    if log:
        log("⬇️ Playwright Chromium 설치 중 (최초 1회, 약 60초)...")
    try:
        result = subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            if log:
                log("✅ Playwright Chromium 설치 완료")
            return True
        if log:
            err = (result.stderr or result.stdout or "")[:200]
            log(f"⚠️ playwright install 실패(rc={result.returncode}): {err}")
    except Exception as e:
        if log:
            log(f"⚠️ playwright install 예외: {str(e)[:100]}")
    return False


def _discover_review_api_via_playwright(goods_no: str, log=None) -> dict | None:
    """
    Playwright으로 상품 페이지를 실제 브라우저로 열고
    리뷰 탭 로딩 시 발생하는 API 요청을 인터셉트한다.

    Returns dict:
        api_url       - 기본 URL (쿼리 제외)
        method        - "GET" | "POST"
        req_headers   - 브라우저가 보낸 요청 헤더
        req_params    - GET 쿼리 파라미터 (dict)
        req_body      - POST 바디 (dict or None)
        page_param    - 페이지 번호 파라미터명 (예: "page" or "pageIdx")
        reviews       - 1페이지 리뷰 목록
        total         - 전체 리뷰 수
    """
    if not _ensure_playwright_browser(log=log):
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    info: dict = {
        "api_url": None, "method": "GET",
        "req_headers": {}, "req_params": {}, "req_body": None,
        "page_param": "page", "reviews": [], "total": 0,
    }

    REVIEW_KWS = ("review", "gdas", "getGdas", "getReview")
    # 리뷰 API가 아닌 URL을 걸러내는 키워드 (상품 페이지 HTML 등)
    REVIEW_EXCL = ("getGoodsDetail", "getGoodsList", "goodsDetail.do", "goodsList.do")

    def _looks_like_review_api_url(url: str) -> bool:
        low = url.lower()
        if any(ext in low for ext in (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico")):
            return False
        if any(excl.lower() in low for excl in REVIEW_EXCL):
            return False
        return any(kw.lower() in low for kw in REVIEW_KWS)

    def _on_request(request):
        """XHR/fetch 요청 디버그 로그 (모든 XHR을 기록)."""
        if request.resource_type not in ("xhr", "fetch"):
            return
        url = request.url
        if log:
            log(f"🌐 XHR [{request.method}]: {url[:120]}")

    def _on_response(response):
        """XHR 응답에서 총 리뷰 수와 리뷰 목록을 캡처한다."""
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        if response.status != 200:
            return
        url = response.url
        if not _looks_like_review_api_url(url):
            return

        try:
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = response.json()
        except Exception:
            return

        # ── 총 리뷰 수: /stats 또는 /count 엔드포인트에서 먼저 추출 ──
        if not info["total"] and isinstance(body, dict):
            for k in ("totalCnt", "totalCount", "total", "totalReviewCount",
                      "allCnt", "reviewCount", "count", "allReviewCnt"):
                v = body.get(k)
                if v is None:
                    # 1단계 중첩 탐색 (e.g. {"data": {"totalCnt": 500}})
                    data_node = body.get("data")
                    if isinstance(data_node, dict):
                        v = data_node.get(k)
                if v is not None:
                    try:
                        info["total"] = int(v)
                        if log:
                            log(f"📊 총 리뷰 수 확인 ({url.split('/')[-1]}): {info['total']}개")
                    except Exception:
                        pass
                    break

        # ── 리뷰 목록 캡처 (이미 캡처됐으면 스킵) ──
        if info["reviews"]:
            return

        # checksum 엔드포인트는 낮은 우선순위 (더 나은 엔드포인트가 있을 수 있음)
        is_checksum = "checksum" in url.lower()

        reviews: list = []
        _extract_from_json_structure(body, reviews)

        if not reviews:
            return

        if is_checksum and not info["reviews"]:
            # checksum 응답이지만 다른 엔드포인트 대기 (최대 2초)
            # → 이미 info["reviews"] 없는 경우에만 임시 저장
            pass

        # ✅ 리뷰 데이터가 있는 응답 → 요청 정보 캡처
        info["reviews"] = reviews
        parsed = urlparse(url)
        info["api_url"] = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        info["method"] = request.method
        info["req_headers"] = dict(request.headers)
        if parsed.query:
            info["req_params"] = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if request.method == "POST":
            try:
                body_text = request.post_data or ""
                if body_text.startswith("{"):
                    info["req_body"] = json.loads(body_text)
                else:
                    info["req_body"] = {k: v[0] for k, v in parse_qs(body_text).items()}
            except Exception:
                pass
        # 총 수가 아직 없으면 이 응답에서도 시도
        if not info["total"] and isinstance(body, dict):
            for k in ("totalCnt", "totalCount", "total", "totalReviewCount",
                      "allCnt", "reviewCount", "count"):
                if body.get(k) is not None:
                    try:
                        info["total"] = int(body[k])
                    except Exception:
                        pass
                    break
        if log:
            log(f"✅ 리뷰 응답 캡처: {info['api_url'].split('/')[-1]} "
                f"({info['method']}, {len(reviews)}개, 총 {info['total']}개)")

    product_url = (
        f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do"
        f"?goodsNo={goods_no}&tab=review"
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-extensions",
                ],
            )
            context = browser.new_context(
                user_agent=PC_UA,
                locale="ko-KR",
                extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"},
            )
            page = context.new_page()
            page.on("request", _on_request)
            page.on("response", _on_response)

            if log:
                log("🌐 Playwright 브라우저 실행 중...")

            page.goto(product_url, wait_until="domcontentloaded", timeout=45_000)
            # 리뷰 탭 자동 클릭 시도
            try:
                for sel in [
                    "a[href*='#reviewArea']",
                    "a[href*='tab=review']",
                    "li:has-text('리뷰') > a",
                    "a:has-text('리뷰'):not([href*='javascript'])",
                    "[class*='tab']:has-text('리뷰')",
                ]:
                    try:
                        elem = page.locator(sel).first
                        if elem.is_visible(timeout=2_000):
                            elem.click(timeout=3_000)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # stats/count 응답 대기 (최대 4초) — 총 리뷰 수 확보
            for _ in range(8):
                if info["total"]:
                    break
                page.wait_for_timeout(500)

            # 리뷰 목록 응답 대기 (추가 최대 8초)
            for _ in range(16):
                if info["reviews"]:
                    break
                page.wait_for_timeout(500)

            # 브라우저가 캡처한 총 수 로그
            if log and info["total"]:
                log(f"📊 브라우저 캡처 총 리뷰 수: {info['total']}개")

            browser.close()

    except Exception as e:
        if log:
            log(f"⚠️ Playwright 오류: {str(e)[:100]}")
        return None

    if not info["api_url"]:
        if log:
            log("⚠️ Playwright: 리뷰 API 요청 인터셉트 실패")
        return None

    # 페이지 파라미터명 추론
    combined = dict(info["req_params"])
    if info["req_body"]:
        combined.update(info["req_body"])
    for candidate in ("page", "pageIdx", "pageNum", "currentPage", "pageNo"):
        if candidate in combined:
            info["page_param"] = candidate
            break

    if log:
        log(f"✅ Playwright API 발견: {info['api_url']}")
        log(f"ℹ️ 메서드: {info['method']} | 페이지 파라미터: {info['page_param']}")
        if info["total"]:
            log(f"📊 전체 리뷰: {info['total']}개")

    return info


def _replay_playwright_api(
    session,
    info: dict,
    page: int,
    sort_code: str,
    log=None,
) -> tuple[list, int]:
    """
    _discover_review_api_via_playwright() 결과를 이용해
    curl_cffi 세션으로 특정 페이지를 직접 재현한다.
    """
    api_url = info["api_url"]
    method = info["method"]
    page_param = info["page_param"]

    # 기본 요청 데이터 복사 후 페이지 번호 변경
    if method == "POST" and info["req_body"]:
        body = dict(info["req_body"])
        body[page_param] = page
    else:
        body = dict(info["req_params"])
        body[page_param] = page

    # 헤더 재사용 (브라우저가 보낸 헤더)
    headers = {
        k: v for k, v in info["req_headers"].items()
        if k.lower() not in ("content-length", "host", ":method", ":path",
                             ":scheme", ":authority", "transfer-encoding")
    }

    try:
        if method == "POST":
            resp = session.post(api_url, json=body, headers=headers, timeout=20)
        else:
            resp = session.get(api_url, params=body, headers=headers, timeout=20)

        if resp.status_code != 200:
            return [], 0

        data = resp.json()
        reviews: list = []
        _extract_from_json_structure(data, reviews)
        total = 0
        if isinstance(data, dict):
            api_status = data.get("status")
            if api_status in ("NOT_FOUND", "ERROR", "FAIL", "BAD_REQUEST"):
                if log and page == 2:
                    log(f"⚠️ replay API 오류: {api_status} - {data.get('message','')[:60]}")
                return [], 0
            for k in ("totalCnt", "totalCount", "total", "totalReviewCount"):
                if data.get(k) is not None:
                    try:
                        total = int(data[k])
                    except Exception:
                        pass
                    break
        return reviews, total

    except Exception as e:
        if log and page <= 3:
            log(f"⚠️ replay 오류: {str(e)[:60]}")
        return [], 0


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
            # RSC 내 API URL 패턴 탐색
            for pat in [
                r'(https?://[^\s"\'\\]+/review[^\s"\'\\]{0,80})',
                r'(https?://[^\s"\'\\]+/api[^\s"\'\\]{0,80})',
                r'"(/(?:api|review)/[^\s"\'\\]{3,80})"',
            ]:
                found = re.findall(pat, rsc_text)
                if found:
                    log(f"🔑 RSC API 힌트: {found[:3]}")
                    break
    except Exception:
        pass

    # ── 2. Playwright 브라우저 인터셉트 (1순위) ──
    playwright_info: dict | None = None
    page1_reviews: list = []
    total_count: int = 0
    working_endpoint: str | None = None

    log("🌐 Playwright 브라우저로 리뷰 API 탐색 중...")
    progress(0.1)
    try:
        playwright_info = _discover_review_api_via_playwright(goods_no, log=log)
    except Exception as e:
        log(f"⚠️ Playwright 예외: {str(e)[:80]}")

    if playwright_info and playwright_info.get("reviews"):
        page1_reviews = playwright_info["reviews"]
        total_count = playwright_info.get("total", 0)
        working_endpoint = playwright_info["api_url"]
        log(f"✅ Playwright API 성공: {len(page1_reviews)}개 리뷰")

        # 캡처된 요청 파라미터 로그
        if playwright_info.get("req_body"):
            log(f"ℹ️ 요청 바디: {str(playwright_info['req_body'])[:200]}")
        elif playwright_info.get("req_params"):
            log(f"ℹ️ 쿼리 파라미터: {str(playwright_info['req_params'])[:200]}")

        # ── /checksum 엔드포인트인 경우 전체 리뷰 엔드포인트 시도 ──
        # checksum은 일부 리뷰만 반환하므로, 브라우저 헤더로 진짜 리뷰 목록 호출
        if "/checksum" in working_endpoint:
            log("🔄 checksum 감지 → 전체 리뷰 엔드포인트 시도...")
            _base = working_endpoint.rsplit("/checksum", 1)[0]  # /review/api/v2/reviews
            _hdr = {
                k: v for k, v in playwright_info.get("req_headers", {}).items()
                if k.lower() not in (":method", ":path", ":scheme", ":authority",
                                     "content-length", "transfer-encoding")
            }
            _sort_map = {"date": None, "useful": "USEFUL_SCORE_DESC",
                         "star_desc": "RATING_DESC", "star_asc": "RATING_ASC"}
            _sort_val = _sort_map.get(sort)

            # 시도할 바디 조합 (goodsNumber vs goodsNo, sortCode 등)
            _candidates = [
                {"goodsNumber": goods_no, "page": 1, "size": PAGE_SIZE, "reviewType": "ALL"},
                {"goodsNumber": goods_no, "page": 1, "size": PAGE_SIZE},
                {"goodsNo": goods_no, "page": 1, "size": PAGE_SIZE, "reviewType": "ALL"},
            ]
            if _sort_val:
                _candidates.insert(0, {
                    "goodsNumber": goods_no, "page": 1, "size": PAGE_SIZE,
                    "reviewType": "ALL", "sortCode": _sort_val,
                })

            for _body in _candidates:
                try:
                    _r = session.post(_base, json=_body, headers=_hdr, timeout=15)
                    log(f"🔎 전체리뷰[{_r.status_code}] {_base.split('/')[-1]}: "
                        f"{html_lib.escape(_r.text[:80].replace(chr(10),' '))}")
                    if _r.status_code == 200:
                        _d = _r.json()
                        _api_st = _d.get("status") if isinstance(_d, dict) else None
                        if _api_st in ("NOT_FOUND", "BAD_REQUEST", "ERROR", "FAIL",
                                       "METHOD_NOT_ALLOWED"):
                            continue
                        _revs: list = []
                        _extract_from_json_structure(_d, _revs)
                        if _revs:
                            log(f"✅ 전체 리뷰 엔드포인트 성공! ({len(_revs)}개)")
                            playwright_info["api_url"] = _base
                            playwright_info["req_body"] = _body
                            playwright_info["req_params"] = {}
                            playwright_info["method"] = "POST"
                            playwright_info["page_param"] = "page"
                            page1_reviews = _revs
                            working_endpoint = _base
                            # 전체 수 재확인
                            if isinstance(_d, dict):
                                for _k in ("totalCnt", "totalCount", "total"):
                                    if _d.get(_k):
                                        try:
                                            total_count = int(_d[_k])
                                        except Exception:
                                            pass
                                        break
                            break
                except Exception as _e:
                    log(f"⚠️ 전체리뷰 시도 오류: {str(_e)[:60]}")
                    continue
    else:
        if playwright_info:
            log("⚠️ Playwright: API 발견했으나 리뷰 0개")
        else:
            log("⚠️ Playwright 실패. 기존 API 시도...")

        # ── 2b. PC GDAS API 직접 호출 ──
        progress(0.2)
        page1_reviews, total_count, working_endpoint = _try_review_api(
            session, goods_no, 1, sort, product_url, html_text=html_text, log=log,
        )

        if working_endpoint:
            log(f"✅ API 엔드포인트: {working_endpoint.split('/')[-1]}")
        else:
            # JS 번들에서 엔드포인트 탐색
            log("⚠️ 기본 API 실패. JS 번들 탐색 중...")
            progress(0.3)
            discovered_ep = _scan_js_for_review_api(session, html_text, rsc_text=rsc_text, log=log)

            if discovered_ep:
                page1_reviews, total_count, working_endpoint = _try_review_api(
                    session, goods_no, 1, sort, product_url,
                    endpoint=discovered_ep, html_text=html_text, log=log,
                )
                if working_endpoint:
                    log(f"✅ JS 번들 API 성공: {working_endpoint.split('/')[-1]}")

        # ── 2c. 모바일 API 폴백 ──
        if not working_endpoint:
            log("🔍 모바일 API 시도 중 (m.oliveyoung.co.kr)...")
            progress(0.4)
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
        use_playwright_replay = playwright_info is not None and playwright_info.get("api_url") == working_endpoint

        for page_idx in range(2, estimated_pages + 1):
            if use_playwright_replay and playwright_info:
                page_reviews, _ = _replay_playwright_api(session, playwright_info, page_idx, sort)
            elif is_mobile_ep:
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

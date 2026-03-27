"""
올리브영 리뷰 크롤러 - 순수 HTTP 버전 (Playwright 없음)
Streamlit Cloud 배포용

전략:
1. 상품 페이지 HTML 파싱 (JSON-LD / 인라인 JS / HTML 구조)
2. 페이지 내 AJAX 엔드포인트 발견 시 추가 페이지 수집
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
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "User-Agent": PC_UA,
}
AJAX_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.oliveyoung.co.kr",
    "Referer": "https://www.oliveyoung.co.kr/",
    "User-Agent": PC_UA,
    "X-Requested-With": "XMLHttpRequest",
}

SORT_MAP = {
    "최신순": "date",
    "추천순": "useful",
    "별점높은순": "star_desc",
    "별점낮은순": "star_asc",
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


def _parse_review_dict(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    review = {}

    for k in ["reviewContent", "gdasContent", "content", "contText", "reviewText",
              "reviewBody", "body", "text"]:
        if item.get(k) and str(item[k]).strip():
            review["content"] = str(item[k]).strip()
            break

    for k in ["reviewScore", "gdasStar", "rating", "score", "starScore", "starPoint",
              "ratingValue"]:
        if item.get(k) is not None:
            review["rating"] = str(item[k])
            break
    # JSON-LD reviewRating 중첩
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


def _parse_nextjs_data(html: str) -> tuple[list[dict], int]:
    """Next.js __next_f.push / __NEXT_DATA__ 에서 리뷰 탐색"""
    reviews = []
    total_count = 0

    # 1. Pages Router: <script id="__NEXT_DATA__">
    nd_match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd_match:
        try:
            data = json.loads(nd_match.group(1))
            _extract_from_json_structure(data, reviews)
        except Exception:
            pass

    # 2. App Router: self.__next_f.push([...])
    for m in re.finditer(r'self\.__next_f\.push\(\[(\d+),\s*"([\s\S]*?)"\]\)', html):
        chunk_type = m.group(1)
        if chunk_type not in ("1", "2"):
            continue
        try:
            raw = m.group(2).encode().decode("unicode_escape")
        except Exception:
            raw = m.group(2)
        try:
            data = json.loads(raw)
            _extract_from_json_structure(data, reviews)
            if isinstance(data, dict):
                tc = data.get("totalCnt") or data.get("totalCount")
                if tc and str(tc).isdigit():
                    total_count = int(tc)
        except Exception:
            pass

    # 3. 인라인 JSON 블록 탐색 (Next.js가 <script> 안에 초기 상태로 넣을 때)
    for m in re.finditer(r'<script[^>]*>\s*window\.__(?:INITIAL|PRELOADED)_STATE__\s*=\s*([\s\S]{20,}?)\s*</script>', html):
        try:
            data = json.loads(m.group(1).rstrip(";"))
            _extract_from_json_structure(data, reviews)
        except Exception:
            pass

    return reviews, total_count


def _parse_html_for_reviews(html: str) -> tuple[list[dict], str | None, int]:
    """
    HTML에서 리뷰 파싱
    Returns: (reviews, ajax_url, total_count)
    """
    reviews = []
    ajax_url = None
    total_count = 0

    # ── 0. Next.js 데이터 탐색 ──
    nj_reviews, nj_total = _parse_nextjs_data(html)
    reviews.extend(nj_reviews)
    if nj_total:
        total_count = nj_total

    # ── 1. JSON-LD 구조화 데이터 ──
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    ):
        try:
            data = json.loads(m.group(1))
            items = data if isinstance(data, list) else [data]
            for item in items:
                _extract_from_json_structure(item, reviews)
        except Exception:
            pass

    # ── 2. 인라인 JS 변수 탐색 ──
    if not reviews:
        js_var_patterns = [
            r'(?:var\s+|let\s+|const\s+)?(?:reviewList|gdasList|reviewData|reviews)\s*[=:]\s*(\[[\s\S]{10,5000}?\])\s*[,;}\n]',
            r'"(?:reviewList|gdasList|reviews)"\s*:\s*(\[[\s\S]{10,5000}?\])',
        ]
        for pattern in js_var_patterns:
            match = re.search(pattern, html)
            if match:
                try:
                    items = json.loads(match.group(1))
                    if isinstance(items, list):
                        for item in items:
                            r = _parse_review_dict(item)
                            if r:
                                reviews.append(r)
                        if reviews:
                            break
                except Exception:
                    pass

    # ── 3. HTML review 요소 탐색 ──
    if not reviews:
        # 별점 + 리뷰 텍스트 패턴으로 추출
        review_blocks = re.findall(
            r'(?:class="(?:review_cont|review_item|gdas_item|review-item)[^"]*"[^>]*>)'
            r'([\s\S]{50,2000}?)'
            r'(?=class="(?:review_cont|review_item|gdas_item|review-item)|</ul>|</div>\s*</div>)',
            html
        )
        for block in review_blocks[:50]:
            review = {}
            # 별점
            star_m = re.search(r'(?:data-score|data-star|star_rating)[^"]*"(\d+)"', block)
            if not star_m:
                star_m = re.search(r'(?:class="star[_-]?\w*")[^>]*>\s*(\d+)', block)
            if star_m:
                review["rating"] = star_m.group(1)

            # 리뷰 내용 (태그 제거)
            text_m = re.search(
                r'class="(?:review_cont_text|review_text|gdas_cont|cont|review-text)[^"]*"[^>]*>([\s\S]{5,}?)</(?:p|div|span)>',
                block
            )
            if text_m:
                review["content"] = re.sub(r'<[^>]+>', '', text_m.group(1)).strip()

            if review.get("content") and len(review["content"]) > 5:
                reviews.append(review)

    # ── 4. 전체 리뷰 수 탐색 ──
    total_patterns = [
        r'"totalCnt"\s*:\s*(\d+)',
        r'"totalCount"\s*:\s*(\d+)',
        r'id="reviewCount"[^>]*>\s*([\d,]+)',
        r'class="(?:review_count|review-count|total_count)[^"]*"[^>]*>([\d,]+)',
        r'총\s*([\d,]+)\s*개',
    ]
    for pattern in total_patterns:
        m = re.search(pattern, html)
        if m:
            try:
                total_count = int(m.group(1).replace(",", ""))
                break
            except Exception:
                pass

    # ── 5. AJAX 엔드포인트 탐색 (인라인 JS) ──
    ajax_patterns = [
        r'["\`]([/][^"\'`\s]{5,80}?(?:gdas|review|Gdas|Review)[^"\'`\s]*?\.do)["\`]',
        r'url\s*[=:]\s*["\']([^"\']{10,80}?(?:gdas|review)[^"\']*?)["\']',
    ]
    for pattern in ajax_patterns:
        matches = re.findall(pattern, html, re.IGNORECASE)
        for candidate in matches:
            if any(k in candidate.lower() for k in ["list", "search", "get"]):
                ajax_url = (
                    candidate if candidate.startswith("http")
                    else f"https://www.oliveyoung.co.kr{candidate}"
                )
                break
        if ajax_url:
            break

    return reviews, ajax_url, total_count


def _fetch_ajax_page(session, ajax_url: str, goods_no: str, page: int, sort: str) -> list[dict]:
    """발견된 AJAX URL로 추가 페이지 수집"""
    param_variants = [
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE, "sort": sort},
        {"goodsNo": goods_no, "page": page, "size": PAGE_SIZE, "sort": sort},
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE},
    ]
    for params in param_variants:
        try:
            resp = session.get(ajax_url, params=params, headers=AJAX_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            reviews = []
            _extract_from_json_structure(data, reviews)
            if reviews:
                return reviews
        except Exception:
            pass
        try:
            resp = session.post(ajax_url, data=params, headers=AJAX_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            reviews = []
            _extract_from_json_structure(data, reviews)
            if reviews:
                return reviews
        except Exception:
            pass
    return []


def deduplicate_reviews(reviews):
    seen = set()
    result = []
    for r in reviews:
        key = r.get("content", "")[:50]
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


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

    # 1. 상품 페이지 HTML 수집 (?tab=review 로 접속)
    log("📦 상품 페이지 수집 중...")
    progress(0.05)
    product_url = (
        f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do"
        f"?goodsNo={goods_no}&tab=review"
    )
    try:
        resp = session.get(product_url, headers=BASE_HEADERS, timeout=20)
        html_text = resp.text if resp.status_code == 200 else ""
    except Exception as e:
        log(f"❌ 상품 페이지 접속 실패: {str(e)[:80]}")
        progress(1.0)
        return "", []

    # RSC 전용 응답 시도 (Next.js App Router)
    rsc_html = ""
    try:
        rsc_resp = session.get(
            product_url,
            headers={**BASE_HEADERS, "RSC": "1", "Next-Router-Prefetch": "1"},
            timeout=15,
        )
        if rsc_resp.status_code == 200:
            rsc_html = rsc_resp.text
    except Exception:
        pass

    # 상품명 추출
    product_name = ""
    title_m = re.search(r"<title>(.*?)</title>", html_text, re.DOTALL)
    if title_m:
        t = title_m.group(1).strip()
        for sep in [" | ", " - ", " │ "]:
            if sep in t:
                product_name = t.split(sep)[0].strip()
                break
    if not product_name:
        nm = re.search(r'class="prd_name[^"]*"[^>]*>(.*?)</[^>]+>', html_text, re.DOTALL)
        if nm:
            product_name = re.sub(r"<[^>]+>", "", nm.group(1)).strip()

    if product_name:
        log(f"✅ 상품명: {product_name}")
    else:
        log("⚠️ 상품명을 가져오지 못했습니다.")

    # 2. HTML 구조 진단
    log(f"ℹ️ HTML 크기: {len(html_text)} chars")
    if rsc_html:
        log(f"ℹ️ RSC 응답: {len(rsc_html)} chars → {html_lib.escape(rsc_html[:200])}")

    # /_next/data/ 엔드포인트 시도 (Pages Router)
    nextdata_reviews = []
    build_id_m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html_text)
    if not build_id_m:
        build_id_m = re.search(r'/_next/static/([A-Za-z0-9_-]{10,}?)/_buildManifest', html_text)
    if build_id_m:
        build_id = build_id_m.group(1)
        log(f"🔑 Next.js buildId: {build_id}")
        nextdata_url = (
            f"https://www.oliveyoung.co.kr/_next/data/{build_id}"
            f"/store/goods/getGoodsDetail.json"
        )
        try:
            nd_resp = session.get(
                nextdata_url,
                params={"goodsNo": goods_no},
                headers={**AJAX_HEADERS, "x-nextjs-data": "1"},
                timeout=15,
            )
            log(f"ℹ️ /_next/data/ 응답: HTTP {nd_resp.status_code}")
            if nd_resp.status_code == 200:
                nd_data = nd_resp.json()
                _extract_from_json_structure(nd_data, nextdata_reviews)
                log(f"📄 /_next/data/: {len(nextdata_reviews)}개 리뷰")
        except Exception as e:
            log(f"⚠️ /_next/data/ 실패: {str(e)[:60]}")
    else:
        # HTML 앞부분 디버그 출력
        preview = html_lib.escape(html_text[:500].replace("\n", " "))
        log(f"🔎 HTML 앞부분: {preview}")

    # HTML + RSC에서 리뷰 파싱
    log("🔍 HTML/RSC에서 리뷰 데이터 탐색 중...")
    progress(0.15)
    combined_html = html_text + rsc_html
    page1_reviews, ajax_url, total_count = _parse_html_for_reviews(combined_html)
    page1_reviews.extend(nextdata_reviews)

    if ajax_url:
        log(f"🔗 AJAX 엔드포인트 발견: {ajax_url}")
    if total_count:
        log(f"📊 전체 리뷰 수: {total_count}개")
    log(f"📄 1페이지: HTML에서 {len(page1_reviews)}개 리뷰 파싱")

    # HTML 구조 디버그 (리뷰가 없을 때)
    if not page1_reviews:
        # 리뷰 관련 키워드 포함 HTML 조각 탐색
        keywords = ["review", "gdas", "별점", "리뷰"]
        for kw in keywords:
            if kw.lower() in html_text.lower():
                idx = html_text.lower().find(kw.lower())
                snippet = html_text[max(0, idx-50):idx+200]
                snippet_clean = re.sub(r'<[^>]+>', ' ', snippet)[:150].strip()
                log(f"🔎 '{kw}' 발견 주변: {html_lib.escape(snippet_clean)}")
                break

    all_reviews = list(page1_reviews)

    # 3. AJAX URL 발견 시 추가 페이지 수집
    if ajax_url and total_count > PAGE_SIZE:
        estimated_pages = min(max_pages, -(-total_count // PAGE_SIZE))
        log(f"📋 추가 페이지 수집 시작 (최대 {estimated_pages}페이지)...")
        consecutive_empty = 0

        for page_idx in range(2, estimated_pages + 1):
            page_reviews = _fetch_ajax_page(session, ajax_url, goods_no, page_idx, sort)

            before = len(all_reviews)
            for r in page_reviews:
                key = r.get("content", "")[:50]
                if not any(e.get("content", "")[:50] == key for e in all_reviews):
                    all_reviews.append(r)
            new = len(all_reviews) - before

            if page_idx <= 5 or page_idx % 10 == 0:
                log(f"📄 페이지 {page_idx}: 수집 {len(page_reviews)}개 | 신규 {new}개 | 누적 {len(all_reviews)}개")

            progress(0.2 + 0.75 * (page_idx / estimated_pages))

            if not page_reviews or new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log(f"✅ 리뷰 끝! (페이지 {page_idx})")
                    break
            else:
                consecutive_empty = 0

            time.sleep(random.uniform(0.3, 0.7))

    all_reviews = deduplicate_reviews(all_reviews)
    progress(1.0)
    log(f"🎉 수집 완료! 총 {len(all_reviews)}개 리뷰")
    return product_name, all_reviews

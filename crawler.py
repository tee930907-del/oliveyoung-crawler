"""
올리브영 리뷰 크롤러 - 순수 HTTP 버전 (Playwright 없음)
Streamlit Cloud 배포용

curl_cffi로 Cloudflare 우회 + 올리브영 리뷰 API v2 호출
"""

import json
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
REVIEW_API_URL = "https://m.oliveyoung.co.kr/review/api/v2/reviews"
PAGE_SIZE = 10
MAX_PAGES = 200

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.oliveyoung.co.kr",
    "Referer": "https://www.oliveyoung.co.kr/",
}


def _create_session():
    """Cloudflare 우회 가능한 세션 생성"""
    if HAS_CURL_CFFI:
        return cf_requests.Session(impersonate="chrome")
    else:
        session = cf_requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        })
        return session


def extract_goods_no(url: str) -> str | None:
    """상품 URL에서 goodsNo 추출"""
    # URL 파라미터에서 추출
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "goodsNo" in params:
        return params["goodsNo"][0]

    # URL 경로에서 추출 (모바일 URL 패턴)
    match = re.search(r"[/=](A\d{12})", url)
    if match:
        return match.group(1)

    return None


def fetch_product_name(session, goods_no: str) -> str:
    """상품명 가져오기 (curl_cffi 세션 사용)"""
    url = (
        f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do"
        f"?goodsNo={goods_no}"
    )
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return ""

        html = resp.text

        # <title> 태그에서 추출
        title_match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        if title_match:
            title_text = title_match.group(1).strip()
            # "상품명 - 올리브영" 또는 "상품명 | 올리브영" 형태
            for sep in [" | ", " - ", " │ "]:
                if sep in title_text:
                    return title_text.split(sep)[0].strip()

        # prd_name 클래스에서 추출
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


def extract_reviews_from_json(data, depth=0):
    """JSON 응답에서 리뷰 데이터 추출"""
    if depth > 10:
        return []
    reviews = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                review = try_parse_review_dict(item)
                if review:
                    reviews.append(review)
                else:
                    reviews.extend(extract_reviews_from_json(item, depth + 1))
        return reviews

    if isinstance(data, dict):
        for key in [
            "reviewList", "reviews", "list", "data", "items", "contents",
            "gdasList", "commentList", "content", "result", "body", "payload"
        ]:
            if key in data:
                val = data[key]
                if isinstance(val, list) and len(val) > 0:
                    sub = extract_reviews_from_json(val, depth + 1)
                    if sub:
                        reviews.extend(sub)
                elif isinstance(val, dict):
                    sub = extract_reviews_from_json(val, depth + 1)
                    if sub:
                        reviews.extend(sub)
        if not reviews:
            review = try_parse_review_dict(data)
            if review:
                reviews.append(review)

    return reviews


def try_parse_review_dict(item):
    """딕셔너리에서 리뷰 정보 파싱"""
    if not isinstance(item, dict):
        return None

    review = {}

    # 리뷰 내용
    for k in [
        "reviewContent", "content", "contText", "reviewText",
        "comment", "body", "text", "rvwCntn", "gdasCont"
    ]:
        if k in item and item[k] and str(item[k]).strip():
            review["content"] = str(item[k]).strip()
            break

    # 별점
    for k in [
        "reviewScore", "rating", "score", "star", "starScore",
        "point", "rvwScore", "starPoint"
    ]:
        if k in item and item[k] is not None:
            review["rating"] = str(item[k])
            break

    # 작성자 (profileDto 중첩 우선)
    if isinstance(item.get("profileDto"), dict):
        profile = item["profileDto"]
        for k in ["memberNickname", "nickname", "nickName", "memberNickName"]:
            if k in profile and profile[k]:
                review["author"] = str(profile[k]).strip()
                break

    if "author" not in review:
        for k in [
            "nickname", "userId", "userName", "author", "writer",
            "membNickName", "nickName", "reviewerName", "memberNickname"
        ]:
            if k in item and item[k]:
                review["author"] = str(item[k]).strip()
                break

    # 작성일
    for k in [
        "createdDateTime", "createDate", "regDate", "date",
        "writtenDate", "createdAt", "rvwDate", "reviewDate",
        "registDate", "rgstDate"
    ]:
        if k in item and item[k]:
            review["date"] = str(item[k]).strip()
            break

    # 옵션 (goodsDto 중첩)
    if isinstance(item.get("goodsDto"), dict):
        goods = item["goodsDto"]
        for k in ["optionName", "goodsName", "optNm"]:
            if k in goods and goods[k] and str(goods[k]).strip():
                review["option"] = str(goods[k]).strip()
                break

    if "option" not in review:
        for k in ["optionName", "option", "goodsOption", "optNm", "selOptNm"]:
            if k in item and item[k] and str(item[k]).strip():
                review["option"] = str(item[k]).strip()
                break

    # 도움수
    for k in [
        "recommendCount", "helpCount", "helpful", "likeCount",
        "usefulPoint", "likeCnt", "helpCnt"
    ]:
        if k in item and item[k] is not None:
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
    "최신순": None,              # sortType 없으면 기본 최신순
    "추천순": "USEFUL_SCORE_DESC",
    "별점높은순": "RATING_DESC",
    "별점낮은순": "RATING_ASC",
}


def crawl_reviews(
    goods_no: str,
    max_pages: int = MAX_PAGES,
    sort_type: str | None = None,
    progress_callback=None,
    log_callback=None,
):
    """
    올리브영 리뷰 크롤링 (순수 HTTP + curl_cffi)

    Args:
        goods_no: 상품번호 (예: "A000000235192")
        max_pages: 최대 페이지 수
        sort_type: 정렬 타입 (REGIST_DESC, USEFUL_SCORE_DESC, etc.)
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

    # 세션 생성
    session = _create_session()

    # 1. 상품 페이지 접속 (쿠키 + 상품명)
    log("📦 상품 정보를 가져오는 중...")
    progress(0.05)
    product_name = fetch_product_name(session, goods_no)
    if product_name:
        log(f"✅ 상품명: {product_name}")
    else:
        log("⚠️ 상품명을 가져오지 못했습니다. (크롤링은 계속됩니다)")

    # 2. 리뷰 API 호출
    log("🔍 리뷰를 수집하는 중...")
    all_reviews = []
    consecutive_empty = 0

    for page_idx in range(1, max_pages + 1):
        req_body = {
            "goodsNumber": goods_no,
            "page": page_idx,
            "size": PAGE_SIZE,
            "reviewType": "ALL",
        }
        if sort_type:
            req_body["sortType"] = sort_type

        try:
            resp = session.post(
                REVIEW_API_URL,
                json=req_body,
                timeout=15,
                headers=HEADERS,
            )

            if resp.status_code != 200:
                consecutive_empty += 1
                log(f"⚠️ 페이지 {page_idx}: HTTP {resp.status_code}")
                if consecutive_empty >= 3:
                    log(f"⚠️ 연속 오류로 수집 종료 (페이지 {page_idx})")
                    break
                continue

            data = resp.json()

            # API 에러 체크
            if data.get("status") != "SUCCESS":
                consecutive_empty += 1
                msg = data.get("message", "알 수 없는 오류")
                log(f"⚠️ 페이지 {page_idx}: {msg}")
                if consecutive_empty >= 3:
                    break
                continue

            page_reviews = extract_reviews_from_json(data)

            # 중복 제거 후 추가
            before = len(all_reviews)
            for r in page_reviews:
                key = r.get("content", "")[:50]
                if not any(e.get("content", "")[:50] == key for e in all_reviews):
                    all_reviews.append(r)
            new = len(all_reviews) - before

            # 진행률 업데이트
            if page_idx <= 5 or page_idx % 10 == 0:
                log(
                    f"📄 페이지 {page_idx}: "
                    f"수집 {len(page_reviews)}개 | "
                    f"신규 {new}개 | "
                    f"누적 {len(all_reviews)}개"
                )

            progress(0.1 + 0.85 * (page_idx / max_pages))

            if len(page_reviews) == 0 or new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log(f"✅ 리뷰 끝! (페이지 {page_idx})")
                    break
            else:
                consecutive_empty = 0

            # 서버 부담 줄이기
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

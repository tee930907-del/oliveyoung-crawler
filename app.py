"""
올리브영 리뷰 크롤러 - Streamlit 웹앱
"""

import streamlit as st
import pandas as pd
import io
from datetime import datetime
from crawler import crawl_reviews, extract_goods_no, SORT_MAP


# ──────────── 페이지 설정 ────────────
st.set_page_config(
    page_title="올리브영 리뷰 크롤러",
    page_icon="🫒",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ──────────── 커스텀 CSS ────────────
st.markdown("""
<style>
    /* 메인 컨테이너 */
    .main .block-container {
        max-width: 900px;
        padding-top: 2rem;
    }

    /* 헤더 영역 */
    .header-container {
        text-align: center;
        padding: 2rem 0 1rem;
    }
    .header-emoji {
        font-size: 3.5rem;
        margin-bottom: 0.5rem;
    }
    .header-title {
        font-size: 2rem;
        font-weight: 800;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 0.3rem;
    }
    .header-sub {
        color: #888;
        font-size: 0.95rem;
    }

    /* 입력 카드 */
    .input-card {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 16px;
        padding: 1.5rem 2rem;
        margin: 1.5rem 0;
    }

    /* 결과 통계 */
    .stat-card {
        background: linear-gradient(135deg, rgba(102, 126, 234, 0.15), rgba(118, 75, 162, 0.15));
        border: 1px solid rgba(102, 126, 234, 0.3);
        border-radius: 12px;
        padding: 1.2rem;
        text-align: center;
    }
    .stat-number {
        font-size: 2rem;
        font-weight: 800;
        color: #667eea;
    }
    .stat-label {
        color: #999;
        font-size: 0.85rem;
        margin-top: 0.2rem;
    }

    /* 로그 영역 */
    .log-container {
        background: rgba(0, 0, 0, 0.3);
        border: 1px solid rgba(255, 255, 255, 0.05);
        border-radius: 12px;
        padding: 1rem 1.2rem;
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 0.82rem;
        line-height: 1.6;
        max-height: 300px;
        overflow-y: auto;
        color: #a0a0a0;
    }

    /* 버튼 스타일 */
    .stButton > button {
        width: 100%;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border: none;
        padding: 0.7rem 2rem;
        border-radius: 10px;
        font-size: 1rem;
        font-weight: 600;
        transition: all 0.3s ease;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
    }

    /* 다운로드 버튼 */
    .stDownloadButton > button {
        width: 100%;
        background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%);
        color: white;
        border: none;
        padding: 0.7rem 2rem;
        border-radius: 10px;
        font-size: 1rem;
        font-weight: 600;
    }
    .stDownloadButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 6px 20px rgba(56, 239, 125, 0.3);
    }

    /* 비밀번호 잠금 화면 */
    .lock-screen {
        text-align: center;
        padding: 4rem 2rem;
    }
    .lock-emoji {
        font-size: 4rem;
        margin-bottom: 1rem;
    }

    /* 구분선 */
    hr {
        border: none;
        border-top: 1px solid rgba(255, 255, 255, 0.06);
        margin: 1.5rem 0;
    }

    /* 데이터프레임 */
    .stDataFrame {
        border-radius: 12px;
        overflow: hidden;
    }

    /* footer 숨기기 */
    footer {visibility: hidden;}
    #MainMenu {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


# ──────────── 비밀번호 보호 ────────────
def check_password():
    """비밀번호 확인 - secrets.toml에서 password 읽기"""
    # secrets에 password가 없으면 비밀번호 보호 비활성화
    if "password" not in st.secrets:
        return True

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    # 잠금 화면
    st.markdown("""
    <div class="lock-screen">
        <div class="lock-emoji">🔒</div>
        <h2>비밀번호를 입력해주세요</h2>
    </div>
    """, unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        password = st.text_input(
            "비밀번호",
            type="password",
            placeholder="비밀번호 입력...",
            label_visibility="collapsed",
        )
        if st.button("🔓 입장", use_container_width=True):
            if password == st.secrets["password"]:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("❌ 비밀번호가 틀렸습니다.")

    return False


# ──────────── 메인 앱 ────────────
def main():
    # 비밀번호 확인
    if not check_password():
        return

    # 헤더
    st.markdown("""
    <div class="header-container">
        <div class="header-emoji">🫒</div>
        <div class="header-title">올리브영 리뷰 크롤러</div>
        <div class="header-sub">상품 URL을 입력하면 리뷰를 자동으로 수집합니다</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # URL 입력
    url = st.text_input(
        "🔗 올리브영 상품 URL",
        placeholder="https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo=A000000235192",
        help="올리브영 상품 페이지 URL을 붙여넣기 해주세요",
    )

    # 옵션
    col1, col2 = st.columns(2)
    with col1:
        max_pages = st.number_input(
            "📄 최대 페이지 수",
            min_value=1,
            max_value=500,
            value=200,
            help="페이지당 10개 리뷰. 200페이지 = 최대 2,000개",
        )
    with col2:
        sort_option = st.selectbox(
            "📊 정렬 기준",
            options=["최신순", "추천순", "별점높은순", "별점낮은순"],
            index=0,
        )

    # SORT_MAP is imported from crawler module

    # 크롤링 시작 버튼
    st.markdown("")
    start_btn = st.button("🚀 크롤링 시작", use_container_width=True)

    if start_btn:
        if not url:
            st.warning("⚠️ URL을 입력해주세요!")
            return

        goods_no = extract_goods_no(url)
        if not goods_no:
            st.error("❌ 올리브영 상품 URL에서 상품번호를 추출할 수 없습니다. URL을 확인해주세요.")
            return

        st.markdown("---")

        # 진행 상태 표시
        progress_bar = st.progress(0, text="준비 중...")
        log_area = st.empty()
        logs = []

        def on_progress(value):
            progress_bar.progress(value, text=f"수집 중... {int(value * 100)}%")

        def on_log(msg):
            logs.append(msg)
            # 최근 30개 로그 표시
            display_logs = logs[-30:]
            log_html = "<br>".join(display_logs)
            log_area.markdown(
                f'<div class="log-container">{log_html}</div>',
                unsafe_allow_html=True,
            )

        # 크롤링 실행
        sort_code = SORT_MAP.get(sort_option, None)
        product_name, reviews = crawl_reviews(
            goods_no,
            max_pages=max_pages,
            sort_type=sort_code,
            progress_callback=on_progress,
            log_callback=on_log,
        )

        progress_bar.progress(1.0, text="✅ 수집 완료!")

        if not reviews:
            st.warning("😢 수집된 리뷰가 없습니다. URL을 확인해주세요.")
            return

        # 세션에 결과 저장
        st.session_state.last_result = {
            "product_name": product_name,
            "reviews": reviews,
            "goods_no": goods_no,
        }

    # 결과 표시
    if "last_result" in st.session_state and st.session_state.last_result:
        result = st.session_state.last_result
        product_name = result["product_name"]
        reviews = result["reviews"]
        goods_no = result["goods_no"]

        st.markdown("---")

        # 데이터 정리
        clean_reviews = []
        for r in reviews:
            content = r.get("content", "")
            content = content.replace("\\r\\n", "\n").replace("\\n", "\n")
            clean_reviews.append({
                "상품명": product_name,
                "별점": r.get("rating", ""),
                "작성자": r.get("author", ""),
                "작성일": r.get("date", ""),
                "리뷰내용": content,
                "구매옵션": r.get("option", ""),
                "도움수": r.get("helpful", "0"),
            })

        df = pd.DataFrame(clean_reviews)

        # 통계 카드
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(f"""
            <div class="stat-card">
                <div class="stat-number">{len(df):,}</div>
                <div class="stat-label">수집된 리뷰</div>
            </div>
            """, unsafe_allow_html=True)
        with col2:
            avg_rating = ""
            try:
                ratings = pd.to_numeric(df["별점"], errors="coerce")
                avg = ratings.mean()
                if pd.notna(avg):
                    avg_rating = f"{avg:.1f}"
            except Exception:
                pass
            st.markdown(f"""
            <div class="stat-card">
                <div class="stat-number">⭐ {avg_rating or '-'}</div>
                <div class="stat-label">평균 별점</div>
            </div>
            """, unsafe_allow_html=True)
        with col3:
            st.markdown(f"""
            <div class="stat-card">
                <div class="stat-number">{goods_no[-6:]}</div>
                <div class="stat-label">상품번호</div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("")

        # 상품명 표시
        if product_name:
            st.markdown(f"**📦 상품명:** {product_name}")

        st.markdown("")

        # 데이터 미리보기
        st.subheader("📋 리뷰 미리보기")
        st.dataframe(
            df.head(50),
            use_container_width=True,
            height=400,
        )

        st.markdown("")

        # 다운로드
        st.subheader("📥 다운로드")

        col1, col2 = st.columns(2)

        with col1:
            # 엑셀 다운로드
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df.to_excel(writer, sheet_name="전체 리뷰", index=False)
                df[["리뷰내용"]].to_excel(writer, sheet_name="리뷰 내용만", index=False)
            buffer.seek(0)

            now = datetime.now().strftime("%Y%m%d_%H%M")
            filename = f"oliveyoung_reviews_{goods_no}_{now}.xlsx"

            st.download_button(
                label="📊 엑셀(XLSX) 다운로드",
                data=buffer,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        with col2:
            # CSV 다운로드
            csv_data = df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                label="📄 CSV 다운로드",
                data=csv_data.encode("utf-8-sig"),
                file_name=f"oliveyoung_reviews_{goods_no}_{now}.csv",
                mime="text/csv",
                use_container_width=True,
            )


if __name__ == "__main__":
    main()

"""네이버 뉴스 검색 API 호출과 응답 파싱을 담당한다.

collector.py(배치 수집기)가 기본 사용처이지만, app.py(웹 서버)도
"한 번도 수집된 적 없는 검색어"에 한해 짧은 타임아웃·재시도 없이
1회만 이 모듈을 호출한다(README 5.14 참고). 배치는 느긋하게 재시도할
여유가 있지만, 웹 요청은 사용자를 오래 기다리게 할 수 없으므로
fetch_naver_news()가 timeout/max_retries를 인자로 받게 했다.
"""

import html
import os
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# 이 모듈만 단독으로 임포트되는 경우에도 .env가 로드되도록
# 여기서 직접 호출한다(호출 순서에 의존하지 않기 위함).
load_dotenv()

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"

# 429(요청 과다)와 5xx는 시간이 지나면 회복될 가능성이 있는
# 일시적 오류로 보고 재시도한다. 400/401/403 같은 요청 자체의
# 문제는 재시도해도 결과가 같으므로 바로 실패 처리한다.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class NaverApiError(Exception):
    """재시도 후에도 네이버 뉴스 API 호출이 실패했을 때 발생한다."""


def clean_api_text(text):
    """네이버 API 결과에 포함된 HTML 태그와 엔티티를 정리한다."""

    if not text:
        return ""

    cleaned_text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(cleaned_text).strip()


def get_news_source(link):
    """뉴스 원문 주소에서 언론사 도메인을 추출한다."""

    if not link:
        return "네이버 뉴스"

    try:
        hostname = urlparse(link).hostname

        if not hostname:
            return "네이버 뉴스"

        return hostname.replace("www.", "")
    except ValueError:
        return "네이버 뉴스"


def parse_pub_date(pub_date_text):
    """API가 내려주는 RFC 822 날짜 문자열을 timezone-aware datetime으로 바꾼다.

    DB의 published_at 컬럼이 TIMESTAMPTZ이므로, 문자열로 가공하지 않고
    datetime 객체 그대로 돌려주면 psycopg2가 알아서 변환해 저장한다.
    """

    if not pub_date_text:
        return None

    try:
        return parsedate_to_datetime(pub_date_text)
    except (TypeError, ValueError):
        return None


def fetch_naver_news(query, display=10, sort="date", max_retries=3, timeout=10):
    """네이버 뉴스 검색 API에서 뉴스 목록을 가져온다.

    일시적 오류(네트워크 오류, 429, 5xx)는 지수 백오프로 재시도하고,
    재시도로 해결되지 않는 오류(인증 실패, 잘못된 요청, 응답 파싱 실패)는
    즉시 NaverApiError를 발생시켜 호출자가 실패를 기록하게 한다.

    max_retries=1이면 재시도 없이 딱 한 번만 시도한다(대기 없이 바로
    NaverApiError를 던짐). 웹 요청 경로(app.py)가 사용자를 기다리게
    하지 않으려고 짧은 timeout과 함께 이 값을 쓴다.
    """

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise NaverApiError(
            "NAVER_CLIENT_ID 또는 NAVER_CLIENT_SECRET이 설정되지 않았습니다."
        )

    headers = {
        "X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID,
        "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET,
    }

    params = {
        "query": query,
        "display": display,
        "start": 1,
        "sort": sort,
        "format": "json",
    }

    last_error = None

    for attempt in range(1, max_retries + 1):
        is_last_attempt = attempt == max_retries

        try:
            response = requests.get(
                API_URL,
                headers=headers,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as error:
            last_error = f"네트워크 오류: {error}"

            if is_last_attempt:
                raise NaverApiError(last_error) from error

            time.sleep(2 ** (attempt - 1))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES:
            last_error = f"일시적 서버 오류 (status={response.status_code})"

            if is_last_attempt:
                raise NaverApiError(last_error)

            time.sleep(2 ** (attempt - 1))
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise NaverApiError(f"API 요청 거부됨: {error}") from error

        try:
            data = response.json()
        except ValueError as error:
            raise NaverApiError(f"응답 파싱 오류: {error}") from error

        return data.get("items", [])

    raise NaverApiError(last_error or "알 수 없는 오류")


def convert_api_items(api_items):
    """API 응답을 articles 테이블에 저장할 형태로 변환한다."""

    converted = []

    for item in api_items:
        original_link = (
            item.get("originallink")
            or item.get("link")
            or ""
        )

        if not original_link:
            continue

        converted.append(
            {
                "title": clean_api_text(item.get("title", "")),
                "content": clean_api_text(item.get("description", "")),
                "link": original_link,
                "source": get_news_source(original_link),
                "published_at": parse_pub_date(item.get("pubDate", "")),
            }
        )

    return converted

"""네이버 뉴스를 주기적으로 수집해 DB에 적재하는 배치 스크립트.

운영 환경에서는 cron(리눅스)이나 작업 스케줄러(윈도우)가 이 스크립트를
일정 주기로 실행한다고 가정한다. 웹 서버(app.py)는 요청을 처리하는
동안 외부 API를 직접 호출하지 않고, 이 스크립트가 채워둔 DB만 조회한다.
수집(배치)과 서빙(웹)을 분리해 API 장애나 지연이 곧바로 사용자 응답
지연으로 이어지지 않게 하는 것이 목적이다.

기본 실행(python collector.py)은 한 번 수집하고 종료한다. cron/작업
스케줄러 없이 로컬에서 바로 반복 수집을 보고 싶을 때는
`python collector.py --loop --interval-minutes 10` 옵션을 사용한다.
"""

import argparse
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

import db
from naver_client import NaverApiError, convert_api_items, fetch_naver_news

load_dotenv()

# 사이트 상단 카테고리 메뉴와 동일한 고정 수집 대상.
FIXED_CATEGORIES = ["스포츠", "연예", "경제", "IT", "사회", "생활문화"]

# 실제 사용자가 많이 검색한 키워드도 수집 대상에 자동으로 편입한다.
TRENDING_KEYWORD_LIMIT = 10

# 한 번도 수집되지 않은 검색어는 인기 순위와 무관하게 무조건 수집한다
# (검색했는데 결과가 계속 비어 있는 상태를 막기 위함). 다만 한 배치에서
# 신규 키워드가 몰릴 경우 API 호출이 급증하지 않도록 상한을 둔다.
NEW_KEYWORD_BATCH_LIMIT = 20

ARTICLES_PER_KEYWORD = 20


def get_collection_targets(connection):
    """고정 카테고리 + 인기 검색어 + 미수집 검색어를 합친 수집 대상 목록을 만든다."""

    trending_rows = connection.execute(
        """
        SELECT keyword
        FROM search_keywords
        ORDER BY count DESC, keyword ASC
        LIMIT %s
        """,
        (TRENDING_KEYWORD_LIMIT,),
    ).fetchall()

    never_collected_rows = connection.execute(
        """
        SELECT keyword
        FROM search_keywords
        WHERE last_collected_at IS NULL
        ORDER BY id ASC
        LIMIT %s
        """,
        (NEW_KEYWORD_BATCH_LIMIT,),
    ).fetchall()

    targets = list(FIXED_CATEGORIES)

    for (keyword,) in trending_rows:
        if keyword not in targets:
            targets.append(keyword)

    for (keyword,) in never_collected_rows:
        if keyword not in targets:
            targets.append(keyword)

    return targets


def collect_one(connection, keyword):
    """키워드 하나를 수집해 DB에 저장하고, 실행 로그를 남긴다."""

    started_at = datetime.now(timezone.utc)

    fetched_count = 0
    inserted_count = 0
    duplicate_count = 0
    status = "success"
    error_message = None

    try:
        api_items = fetch_naver_news(
            query=keyword,
            display=ARTICLES_PER_KEYWORD,
            sort="date",
        )

        articles = convert_api_items(api_items)
        fetched_count = len(articles)

        for article in articles:
            cursor = connection.execute(
                """
                INSERT INTO articles (
                    title, content, link, source, category, published_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (link) DO NOTHING
                """,
                (
                    article["title"],
                    article["content"],
                    article["link"],
                    article["source"],
                    keyword,
                    article["published_at"],
                ),
            )

            if cursor.rowcount:
                inserted_count += 1
            else:
                duplicate_count += 1

        connection.commit()

    except NaverApiError as error:
        connection.rollback()
        status = "failed"
        error_message = str(error)
        print(f"[수집 실패] '{keyword}': {error_message}")

    finished_at = datetime.now(timezone.utc)

    if status == "success":
        # search_keywords에 있는 키워드라면(즉, 사용자가 직접 검색한
        # 키워드라면) 수집 시각을 남겨서 다음부터는 인기 순위로만
        # 재수집 여부가 결정되게 한다. 고정 카테고리처럼 애초에
        # search_keywords에 없는 키워드는 이 UPDATE가 조용히 0건
        # 적용되고 넘어간다.
        connection.execute(
            """
            UPDATE search_keywords
            SET last_collected_at = %s
            WHERE keyword = %s
            """,
            (finished_at, keyword),
        )

    connection.execute(
        """
        INSERT INTO collection_logs (
            keyword, started_at, finished_at,
            fetched_count, inserted_count, duplicate_count,
            status, error_message
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            keyword,
            started_at,
            finished_at,
            fetched_count,
            inserted_count,
            duplicate_count,
            status,
            error_message,
        ),
    )
    connection.commit()

    if status == "success":
        print(
            f"[수집 완료] '{keyword}': "
            f"조회 {fetched_count}건 / 신규 {inserted_count}건 "
            f"/ 중복 {duplicate_count}건"
        )

    return status == "success"


def run_once():
    """수집 대상 전체를 한 바퀴 수집한다. 한 키워드의 실패가 다른
    키워드 수집을 막지 않도록 키워드별로 예외를 격리해서 처리한다."""

    db.init_db()
    connection = db.get_connection()

    targets = get_collection_targets(connection)
    print(f"[수집 시작] 대상 키워드 {len(targets)}개: {', '.join(targets)}")

    success_count = sum(
        collect_one(connection, keyword)
        for keyword in targets
    )

    connection.close()
    print(f"[수집 종료] 성공 {success_count}/{len(targets)}")


def main():
    parser = argparse.ArgumentParser(description="네이버 뉴스 수집 배치")

    parser.add_argument(
        "--loop",
        action="store_true",
        help="지정한 주기로 반복 실행한다 (기본은 1회 실행 후 종료).",
    )

    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=10,
        help="--loop 사용 시 수집 주기(분). 기본값 10분.",
    )

    args = parser.parse_args()

    if not args.loop:
        run_once()
        return

    while True:
        run_once()
        print(f"{args.interval_minutes}분 후 다시 수집합니다...")
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()

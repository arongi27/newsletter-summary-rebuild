"""네이버 뉴스를 주기적으로 수집해 DB에 적재하는 배치 스크립트.

운영 환경에서는 GitHub Actions의 스케줄 워크플로(.github/workflows/
collect.yml)가 이 스크립트를 매시간 1회 실행한다. 웹 서버(app.py)는
평소에는 외부 API를 직접 호출하지 않고 이 스크립트가 채워둔 articles
테이블을 조회하지만, "한 번도 수집된 적 없는 검색어"에 한해서는 속도
제한 안에서 그 자리에서 짧게 API를 불러 즉시 보여준다(app.py의
fetch_and_store_realtime 참고, README 5.14). 이 배치는 그 실시간
조회가 실패했거나 아직 한 번도 검색되지 않은 키워드까지 포함해
넓게 커버하는 안전망 역할이다.

기본 실행(python collector.py)은 한 번 수집하고 종료한다. 로컬에서
반복 수집을 확인하고 싶을 때는
`python collector.py --loop --interval-minutes 10` 옵션을 사용한다.
"""

import argparse
import time
from datetime import datetime, timezone

import psycopg2
from dotenv import load_dotenv

import db
from naver_client import NaverApiError, convert_api_items, fetch_naver_news

load_dotenv()

# 사이트 상단 카테고리 메뉴와 동일한 고정 수집 대상.
FIXED_CATEGORIES = ["스포츠", "연예", "경제", "IT", "사회", "생활문화"]

# 실제 사용자가 많이 검색한 키워드도 수집 대상에 자동으로 편입한다.
TRENDING_KEYWORD_LIMIT = 10

# 한 번도 수집되지 않은 검색어는 인기 순위와 무관하게 무조건 수집한다
# (검색했는데 결과가 계속 비어 있는 상태를 막기 위함). 이제는 웹의
# 실시간 조회가 대부분 먼저 처리하지만, 속도 제한에 걸렸거나 API가
# 실패했던 검색어는 여기로 넘어온다. 다만 한 배치에서 신규 키워드가
# 몰릴 경우 API 호출이 급증하지 않도록 상한을 둔다.
NEW_KEYWORD_BATCH_LIMIT = 20

# 한 번 수집된 검색어라도 사용자가 계속 찾는데 오래 갱신이 안 되면
# 오래된 기사만 계속 보여주게 된다. "최근에 검색됐고(=아직 관심이
# 있고) 마지막 수집이 오래된" 검색어를 재수집 대상에 넣는다.
STALE_SEARCH_WITHIN_DAYS = 7
STALE_RECOLLECT_AFTER_HOURS = 6
STALE_SEARCHED_KEYWORD_LIMIT = 10

ARTICLES_PER_KEYWORD = 20

# 한 배치가 부를 수 있는 최대 API 호출 수(키워드당 1회 호출, 전부
# 겹치지 않는 최악의 경우). 고정 카테고리 + 인기 검색어 + 미수집
# 검색어 + 재수집 대상을 전부 더한 값이다. GitHub Actions 워크플로의
# timeout-minutes(10분)를 넘기지 않는지 이 상수 기준으로 가늠할 수 있다.
MAX_TARGETS_PER_BATCH = (
    len(FIXED_CATEGORIES)
    + TRENDING_KEYWORD_LIMIT
    + NEW_KEYWORD_BATCH_LIMIT
    + STALE_SEARCHED_KEYWORD_LIMIT
)


def get_collection_targets(connection):
    """고정 카테고리 + 인기 검색어 + 미수집 검색어 + 재수집 대상을 합쳐
    수집 대상 목록을 만든다. 최악의 경우 MAX_TARGETS_PER_BATCH개다."""

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

    stale_searched_rows = connection.execute(
        """
        SELECT keyword
        FROM search_keywords
        WHERE last_searched_at > NOW() - (%s * INTERVAL '1 day')
          AND last_collected_at IS NOT NULL
          AND last_collected_at < NOW() - (%s * INTERVAL '1 hour')
        ORDER BY last_searched_at DESC
        LIMIT %s
        """,
        (
            STALE_SEARCH_WITHIN_DAYS,
            STALE_RECOLLECT_AFTER_HOURS,
            STALE_SEARCHED_KEYWORD_LIMIT,
        ),
    ).fetchall()

    targets = list(FIXED_CATEGORIES)

    for (keyword,) in trending_rows:
        if keyword not in targets:
            targets.append(keyword)

    for (keyword,) in never_collected_rows:
        if keyword not in targets:
            targets.append(keyword)

    for (keyword,) in stale_searched_rows:
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

    except (NaverApiError, psycopg2.Error) as error:
        # API 실패뿐 아니라 이 키워드를 저장하다 난 DB 오류도 여기서 격리해
        # 다음 키워드 수집을 계속한다. 연결 자체가 끊긴 경우에는 아래 로그
        # 기록에서 다시 오류가 나고, 그때는 배치 전체가 중단된다.
        connection.rollback()
        status = "failed"
        error_message = f"{type(error).__name__}: {error}"
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


# app.py의 실시간 조회 속도 제한(README 5.14)이 쓰는 로그 테이블.
# app.py도 자리를 예약할 때마다 오래된 행을 지우지만, 그건 "누군가
# 실시간 조회를 시도할 때만" 일어난다. 이 배치는 시도 여부와 무관하게
# 매시간 한 번씩 확실히 청소해서, 오랫동안 아무도 새 키워드를 검색하지
# 않아도 테이블이 무한정 방치되지 않게 한다.
REALTIME_FETCH_LOG_RETENTION_HOURS = 1


def cleanup_realtime_fetch_log(connection):
    cursor = connection.execute(
        """
        DELETE FROM realtime_fetch_log
        WHERE requested_at < NOW() - (%s * INTERVAL '1 hour')
        """,
        (REALTIME_FETCH_LOG_RETENTION_HOURS,),
    )
    connection.commit()
    return cursor.rowcount


def run_once():
    """수집 대상 전체를 한 바퀴 수집한다. API 실패와 키워드 단위 DB
    오류는 키워드별로 격리해서, 한 키워드의 실패가 다른 키워드 수집을
    막지 않게 한다. (DB 연결이 끊기면 배치 전체를 중단한다.)"""

    db.init_db()

    with db.connection() as connection:
        targets = get_collection_targets(connection)
        print(f"[수집 시작] 대상 키워드 {len(targets)}개: {', '.join(targets)}")

        success_count = sum(
            collect_one(connection, keyword)
            for keyword in targets
        )

        deleted_log_count = cleanup_realtime_fetch_log(connection)

    print(f"[수집 종료] 성공 {success_count}/{len(targets)}")
    print(f"[정리] realtime_fetch_log 오래된 행 {deleted_log_count}건 삭제")


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

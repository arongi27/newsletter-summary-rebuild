"""검색 쿼리(articles.title/content ILIKE 검색)의 인덱스 효과를
재현 가능하게 측정하기 위한 벤치마크 스크립트.

실제 수집 데이터는 아직 수백~수천 건 수준이라 인덱스 유무에 따른
차이가 거의 드러나지 않는다. 이 스크립트는 category='벤치마크'로
표시한 대량의 더미 기사를 넣고, 트라이그램 GIN 인덱스를 껐다 켜며
같은 검색 쿼리의 실행 계획(EXPLAIN ANALYZE)과 실행 시간을 비교한다.
더미 데이터는 실제 수집 데이터와 category로 구분되므로 서비스
화면(카테고리 메뉴)에는 노출되지 않는다.

사용법:
    python benchmark_search.py --seed 100000   더미 기사 10만 건 생성
    python benchmark_search.py --run 반도체     before/after 실행계획 비교
    python benchmark_search.py --cleanup       더미 기사 전체 삭제
"""

import argparse
import random
import time
import uuid

import db

BENCH_CATEGORY = "벤치마크"

TRGM_INDEXES = [
    ("idx_articles_title_trgm", "title"),
    ("idx_articles_content_trgm", "content"),
]

# 실제 기사 문장과 완전히 같을 필요는 없다. 목적은 "검색어가 드물게만
# 등장하는 대량의 한글 텍스트"를 만들어 인덱스의 선택도 이점을
# 재현하는 것이다.
FILLER_WORDS = [
    "오늘", "발표", "정책", "시장", "전망", "산업", "기업", "투자",
    "성장", "계획", "회의", "협력", "지원", "확대", "개선", "추진",
    "서비스", "제품", "출시", "행사", "지역", "주민", "학생", "교육",
    "문화", "행정", "예산", "정비", "건설", "환경", "안전", "복지",
    "농업", "관광", "축제", "체육", "대회", "운영", "관리", "점검",
    "협약", "체결", "방문", "참석", "설명", "논의", "검토", "결정",
]

DEFAULT_KEYWORD = "반도체"
TARGET_RATIO = 0.005  # 전체 더미 기사 중 검색어를 포함하는 비율(선택도)


def random_sentence(min_words, max_words):
    return " ".join(
        random.choices(FILLER_WORDS, k=random.randint(min_words, max_words))
    )


def seed(count, keyword=DEFAULT_KEYWORD):
    connection = db.get_connection()
    print(f"[벤치마크] 더미 기사 {count}건 생성 중... (검색어 '{keyword}' 포함 비율 {TARGET_RATIO:.1%})")

    batch_size = 2000
    inserted = 0
    started = time.perf_counter()

    for start in range(0, count, batch_size):
        rows = []

        for _ in range(start, min(start + batch_size, count)):
            title = random_sentence(4, 8)
            content = random_sentence(20, 40)

            if random.random() < TARGET_RATIO:
                title = f"{keyword} {title}"
                content = f"{content} {keyword} 관련 소식입니다"

            rows.append(
                (
                    title,
                    content,
                    f"bench://{uuid.uuid4()}",
                    "벤치마크 언론사",
                    BENCH_CATEGORY,
                )
            )

        connection.execute_values(
            """
            INSERT INTO articles (title, content, link, source, category)
            VALUES %s
            """,
            rows,
        )
        connection.commit()
        inserted += len(rows)
        print(f"  {inserted}/{count}")

    elapsed = time.perf_counter() - started
    print(f"[벤치마크] 생성 완료: {inserted}건, {elapsed:.1f}초")
    connection.close()


def drop_trgm_indexes(connection):
    for name, _ in TRGM_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {name}")
    connection.commit()


def create_trgm_indexes(connection):
    connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for name, column in TRGM_INDEXES:
        connection.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {name}
            ON articles USING GIN ({column} gin_trgm_ops)
            """
        )

    connection.commit()


SEARCH_QUERY = """
    EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
    SELECT id, title, content, published_at, source, link
    FROM articles
    WHERE title ILIKE %s OR content ILIKE %s
    ORDER BY published_at DESC NULLS LAST
    LIMIT 10
"""


def explain_search(connection, keyword):
    pattern = f"%{keyword}%"
    cursor = connection.execute(SEARCH_QUERY, (pattern, pattern))
    plan_lines = [row[0] for row in cursor.fetchall()]
    return "\n".join(plan_lines)


def extract_execution_time(plan_text):
    for line in plan_text.splitlines():
        if line.strip().startswith("Execution Time:"):
            return line.strip()
    return "(찾을 수 없음)"


def run_comparison(keyword):
    connection = db.get_connection()

    print(f"[벤치마크] 검색어 '{keyword}' 기준 before/after 비교\n")

    print("=" * 60)
    print("BEFORE: 트라이그램 인덱스 없음 (title/content Seq Scan)")
    print("=" * 60)
    drop_trgm_indexes(connection)
    before_plan = explain_search(connection, keyword)
    print(before_plan)

    print()
    print("=" * 60)
    print("AFTER: 트라이그램 GIN 인덱스 적용")
    print("=" * 60)
    create_trgm_indexes(connection)
    after_plan = explain_search(connection, keyword)
    print(after_plan)

    connection.close()

    print()
    print("=" * 60)
    print("요약")
    print("=" * 60)
    print("BEFORE:", extract_execution_time(before_plan))
    print("AFTER :", extract_execution_time(after_plan))


def cleanup():
    connection = db.get_connection()
    cursor = connection.execute(
        "DELETE FROM articles WHERE category = %s",
        (BENCH_CATEGORY,),
    )
    deleted = cursor.rowcount
    connection.commit()
    connection.close()
    print(f"[벤치마크] 더미 기사 {deleted}건 삭제 완료")


def main():
    parser = argparse.ArgumentParser(description="검색 쿼리 인덱스 벤치마크")
    parser.add_argument("--seed", type=int, help="더미 기사 N건 생성")
    parser.add_argument("--run", type=str, help="주어진 키워드로 before/after 실행계획 비교")
    parser.add_argument("--cleanup", action="store_true", help="벤치마크용 더미 기사 전체 삭제")

    args = parser.parse_args()

    if args.seed:
        seed(args.seed)
    elif args.run:
        run_comparison(args.run)
    elif args.cleanup:
        cleanup()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

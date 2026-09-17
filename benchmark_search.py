"""검색 쿼리(articles.title/content ILIKE 검색)의 인덱스 효과를
같은 절차로 다시 측정할 수 있게 만든 벤치마크 스크립트.

실제 수집 데이터는 아직 수백~수천 건 수준이라 인덱스 유무에 따른
차이가 거의 드러나지 않는다. 이 스크립트는 category='벤치마크'로
표시한 대량의 더미 기사를 넣고, 트라이그램 GIN 인덱스를 껐다 켜며
같은 검색 쿼리의 실행 계획(EXPLAIN ANALYZE)과 실행 시간을 비교한다.

주의: 더미 기사는 카테고리 메뉴에는 나타나지 않지만, 검색 쿼리에는
카테고리 조건이 없어서 측정하는 동안 검색 결과·추천에 섞일 수 있다.
또 --run은 트라이그램 인덱스를 삭제했다가 다시 만든다. 서비스 중인
DB가 아니라 Neon 브랜치 같은 분리된 DB에서 실행한다.

사용법:
    python benchmark_search.py --seed 100000   더미 기사 10만 건 생성
    python benchmark_search.py --run 반도체     before/after 실행계획 비교
    python benchmark_search.py --cleanup       더미 기사 전체 삭제

--run에 쓸 수 있는 검색어 시나리오(선택도가 서로 다르게 seed()가 미리
심어둔다):
    반도체          흔한 키워드, 0.5% (+ 실제 수집 기사에도 등장 가능)
    벤치희귀어      실제 기사에는 없는 지어낸 단어, 0.01%(더미 10만 건 중 ~10건)
    존재하지않는검색어  더미에도 전혀 심지 않음, 0건(항상 결과 없음)
"""

import argparse
import random
import re
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

import db

BENCH_CATEGORY = "벤치마크"

# 난수 시드를 고정해 몇 번을 생성해도 같은 더미 데이터가 만들어지게 한다.
RANDOM_SEED = 42

# 한 번만 재면 캐시 상태에 따라 결과가 크게 흔들린다. 조건마다 1회
# 워밍업(캐시 채우기) 후 여러 번 실행해 중앙값을 비교한다.
MEASURE_REPEAT = 5

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

# (검색어, 더미 기사 중 포함 비율) — 세 가지 선택도를 동시에 심어서
# 같은 테이블 하나로 "흔한 키워드/아주 드문 키워드/아예 없는 키워드"를
# 전부 비교할 수 있게 한다. 비율이 0이면 절대 심지 않는다(0건 보장).
KEYWORD_SCENARIOS = [
    ("반도체", 0.005),
    ("벤치희귀어", 0.0001),
    ("존재하지않는검색어", 0.0),
]

# 더미 기사의 published_at을 이 범위 안에서 무작위로 채운다. 실제
# 수집 기사와 날짜 범위가 겹쳐야, NULLS LAST 인덱스로 정렬할 때
# 더미 데이터가 항상 맨 뒤로 밀려나지 않고 실제로 섞여서 스캔된다
# (전부 NULL로 두면 항상 맨 뒤로 밀려나 벤치마크가 왜곡된다).
PUBLISHED_AT_RANGE_DAYS = 30


def random_sentence(min_words, max_words):
    return " ".join(
        random.choices(FILLER_WORDS, k=random.randint(min_words, max_words))
    )


def random_published_at(now):
    offset_seconds = random.uniform(0, PUBLISHED_AT_RANGE_DAYS * 24 * 60 * 60)
    return now - timedelta(seconds=offset_seconds)


def seed(count):
    random.seed(RANDOM_SEED)
    now = datetime.now(timezone.utc)

    connection = db.get_connection()
    print(f"[벤치마크] 더미 기사 {count}건 생성 중...")
    print(f"  published_at: 최근 {PUBLISHED_AT_RANGE_DAYS}일 안에서 무작위(시드 고정)")
    for keyword, ratio in KEYWORD_SCENARIOS:
        print(f"  포함 비율 '{keyword}': {ratio:.4%}")

    batch_size = 2000
    inserted = 0
    started = time.perf_counter()

    for start in range(0, count, batch_size):
        rows = []

        for _ in range(start, min(start + batch_size, count)):
            title = random_sentence(4, 8)
            content = random_sentence(20, 40)

            for keyword, ratio in KEYWORD_SCENARIOS:
                if ratio > 0 and random.random() < ratio:
                    title = f"{keyword} {title}"
                    content = f"{content} {keyword} 관련 소식입니다"

            rows.append(
                (
                    title,
                    content,
                    f"bench://{uuid.UUID(int=random.getrandbits(128))}",
                    "벤치마크 언론사",
                    BENCH_CATEGORY,
                    random_published_at(now),
                )
            )

        connection.execute_values(
            """
            INSERT INTO articles (title, content, link, source, category, published_at)
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


def extract_execution_ms(plan_text):
    """실행 계획 마지막의 'Execution Time: 0.991 ms'에서 숫자만 꺼낸다."""

    for line in plan_text.splitlines():
        line = line.strip()

        if line.startswith("Execution Time:"):
            return float(line.split(":")[1].split()[0])

    raise ValueError("실행 계획에서 Execution Time을 찾을 수 없습니다.")


SCAN_NODE_RE = re.compile(
    r"(Seq Scan on articles"
    r"|Bitmap Heap Scan on articles"
    r"|Index Scan using (?P<idx1>\S+) on articles"
    r"|Index Only Scan using (?P<idx2>\S+) on articles)"
)
ACTUAL_ROWS_RE = re.compile(
    r"actual time=[0-9.]+\.\.[0-9.]+ rows=([0-9.]+) loops=(\d+)"
)
ROWS_REMOVED_RE = re.compile(r"Rows Removed by Filter:\s*(\d+)")
BITMAP_INDEX_RE = re.compile(r"Bitmap Index Scan on (\S+)")


def parse_scan_info(plan_text):
    """실행 계획에서 실제 테이블 접근 방식과, LIMIT/정렬 전에 실제로
    들여다본 행 수를 뽑는다.

    - Seq Scan/정렬 인덱스 Scan처럼 Filter로 걸러내는 방식은
      "Rows Removed by Filter" + 실제 반환 행 수를 더한 값이 실제로
      훑은 행 수다.
    - Bitmap Heap Scan은 Recheck Cond로 후보를 이미 좁혀 놓은
      상태라 그 노드의 실제 행 수 자체가 훑은 행 수다(그 뒤에 Sort가
      전체를 다시 정렬한다).
    """

    lines = plan_text.splitlines()

    for i, line in enumerate(lines):
        match = SCAN_NODE_RE.search(line)

        if not match:
            continue

        if match.group("idx1"):
            scan_label = f"Index Scan({match.group('idx1')})"
        elif match.group("idx2"):
            scan_label = f"Index Only Scan({match.group('idx2')})"
        elif "Bitmap Heap Scan" in match.group(0):
            index_names = []

            for detail_line in lines[i + 1 : i + 8]:
                idx_match = BITMAP_INDEX_RE.search(detail_line)

                if idx_match:
                    index_names.append(idx_match.group(1))

            scan_label = f"Bitmap Heap Scan({','.join(index_names) or '?'})"
        else:
            scan_label = "Seq Scan"

        actual_match = ACTUAL_ROWS_RE.search(line)
        node_rows = (
            round(float(actual_match.group(1)) * int(actual_match.group(2)))
            if actual_match
            else None
        )

        rows_removed = None

        for detail_line in lines[i : i + 6]:
            removed_match = ROWS_REMOVED_RE.search(detail_line)

            if removed_match:
                rows_removed = int(removed_match.group(1))
                break

        if rows_removed is not None and node_rows is not None:
            rows_scanned = rows_removed + node_rows
        else:
            rows_scanned = node_rows

        return scan_label, rows_scanned

    return "(알 수 없음)", None


def measure(connection, keyword):
    """워밍업 1회 후 MEASURE_REPEAT회 실행해 마지막 계획과 시간 목록을 반환한다."""

    explain_search(connection, keyword)

    plans = [
        explain_search(connection, keyword)
        for _ in range(MEASURE_REPEAT)
    ]

    times = [extract_execution_ms(plan) for plan in plans]
    return plans[-1], times


def print_result(label, plan, times):
    scan_label, rows_scanned = parse_scan_info(plan)

    print("=" * 60)
    print(label)
    print("=" * 60)
    print(plan)
    print(
        f"\n실행 시간(ms) {MEASURE_REPEAT}회: "
        + ", ".join(f"{t:.3f}" for t in times)
    )
    print(f"중앙값: {statistics.median(times):.3f} ms")
    print(f"접근 방식: {scan_label}")
    print(f"실제로 훑은 행 수(LIMIT/정렬 전): {rows_scanned}\n")

    return scan_label, rows_scanned


def run_comparison(keyword):
    connection = db.get_connection()

    print(f"[벤치마크] 검색어 '{keyword}' 기준 before/after 비교\n")

    # 대량 삽입 직후에는 플래너 통계가 오래되어 잘못된 계획을 고를 수 있다.
    connection.execute("ANALYZE articles")
    connection.commit()

    drop_trgm_indexes(connection)
    before_plan, before_times = measure(connection, keyword)
    before_scan, before_rows = print_result(
        "BEFORE: 트라이그램 인덱스 없음",
        before_plan,
        before_times,
    )

    create_trgm_indexes(connection)
    after_plan, after_times = measure(connection, keyword)
    after_scan, after_rows = print_result(
        "AFTER: 트라이그램 GIN 인덱스 적용",
        after_plan,
        after_times,
    )

    connection.close()

    before_median = statistics.median(before_times)
    after_median = statistics.median(after_times)

    print("=" * 60)
    print("요약 (중앙값)")
    print("=" * 60)
    print(f"BEFORE: {before_median:.3f} ms | {before_scan} | 훑은 행 수 {before_rows}")
    print(f"AFTER : {after_median:.3f} ms | {after_scan} | 훑은 행 수 {after_rows}")
    print(f"개선  : 약 {before_median / after_median:.1f}배")

    return {
        "keyword": keyword,
        "before_median": before_median,
        "before_scan": before_scan,
        "before_rows": before_rows,
        "after_median": after_median,
        "after_scan": after_scan,
        "after_rows": after_rows,
    }


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

"""SQLite 연결과 스키마 초기화를 담당한다.

app.py(웹 서버)와 collector.py(배치 수집기)가 같은 DB 스키마를
공유하기 때문에 커넥션/스키마 관리를 이 모듈로 분리했다.
"""

import sqlite3

DB_PATH = "news.db"


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate_search_keywords_table(connection):
    """
    검색어별 수집 여부를 추적하기 위해 last_collected_at 컬럼을 추가한다.

    이 컬럼이 NULL이면 "한 번도 수집된 적 없는 검색어"라는 뜻이고,
    collector.py는 이런 키워드를 인기 순위와 무관하게 다음 배치에서
    반드시 수집 대상에 포함시킨다.
    """

    columns = connection.execute(
        "PRAGMA table_info(search_keywords)"
    ).fetchall()

    column_names = {
        column[1]
        for column in columns
    }

    if "last_collected_at" not in column_names:
        connection.execute(
            """
            ALTER TABLE search_keywords
            ADD COLUMN last_collected_at TEXT
            """
        )


def migrate_favorites_table(connection):
    """
    기존 즐겨찾기 테이블을 API 뉴스용 구조로 변환한다.

    이전 구조:
    - news_id 기준 중복 확인
    - 원문 링크 없음

    새로운 구조:
    - 원문 링크 저장
    - 사용자와 원문 링크 기준 중복 방지
    """

    table = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'favorites'
        """
    ).fetchone()

    if not table:
        connection.execute(
            """
            CREATE TABLE favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                news_title TEXT NOT NULL,
                news_content TEXT NOT NULL,
                news_date TEXT NOT NULL,
                news_source TEXT NOT NULL,
                news_link TEXT NOT NULL,
                UNIQUE(username, news_link)
            )
            """
        )
        return

    table_sql = table[0] or ""

    columns = connection.execute(
        "PRAGMA table_info(favorites)"
    ).fetchall()

    column_names = {
        column[1]
        for column in columns
    }

    migration_needed = (
        "news_link" not in column_names
        or "UNIQUE(username, news_id)" in table_sql
    )

    if not migration_needed:
        return

    connection.execute(
        """
        CREATE TABLE favorites_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            news_title TEXT NOT NULL,
            news_content TEXT NOT NULL,
            news_date TEXT NOT NULL,
            news_source TEXT NOT NULL,
            news_link TEXT NOT NULL,
            UNIQUE(username, news_link)
        )
        """
    )

    # 기존 즐겨찾기 자료도 삭제하지 않고 보존한다.
    connection.execute(
        """
        INSERT OR IGNORE INTO favorites_new (
            id,
            username,
            news_title,
            news_content,
            news_date,
            news_source,
            news_link
        )
        SELECT
            id,
            username,
            news_title,
            news_content,
            news_date,
            news_source,
            'legacy://favorite/' || id
        FROM favorites
        """
    )

    connection.execute("DROP TABLE favorites")

    connection.execute(
        """
        ALTER TABLE favorites_new
        RENAME TO favorites
        """
    )


def init_db():
    connection = get_connection()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS search_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            count INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    migrate_search_keywords_table(connection)

    migrate_favorites_table(connection)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_search_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            keyword TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(username, keyword)
        )
        """
    )

    # 수집기(collector.py)가 채우고, 웹 서버는 조회만 하는 테이블.
    # link에 UNIQUE 제약을 걸어 같은 기사가 여러 번 수집되어도
    # INSERT OR IGNORE 한 줄로 중복이 걸러지도록 했다.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            published_at TEXT,
            collected_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """
    )

    # 목록/검색 화면이 항상 published_at DESC로 정렬하기 때문에
    # 정렬 기준 컬럼을 인덱스 앞쪽에 둔다.
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_category_published
        ON articles(category, published_at DESC)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_published_at
        ON articles(published_at DESC)
        """
    )

    # 수집 배치가 실행될 때마다 키워드 단위로 실행 결과를 남긴다.
    # 장애가 발생했을 때 "언제, 어떤 키워드에서, 왜" 실패했는지
    # 바로 확인할 수 있게 하기 위한 운영 로그 테이블이다.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            inserted_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error_message TEXT
        )
        """
    )

    connection.commit()
    connection.close()

"""PostgreSQL 연결(풀)과 스키마 초기화를 담당한다.

app.py(웹 서버)와 collector.py(배치 수집기)가 DATABASE_URL 하나로
같은 Postgres 인스턴스(Neon)에 접속한다.
"""

import os

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")

# Neon 같은 서버리스 Postgres는 커넥션을 새로 맺는 비용이 상대적으로
# 크다. 매 요청마다 새로 연결하지 않고 풀에서 커넥션을 빌려 쓰고
# 반납하는 방식으로 연결 비용을 줄인다.
_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=DATABASE_URL,
)


class PooledConnection:
    """psycopg2 커넥션에 sqlite3.Connection과 비슷한 execute() 편의
    메서드를 얹은 얇은 래퍼. close()는 실제 연결을 끊지 않고
    풀에 반납하기만 한다."""

    def __init__(self, raw_connection):
        self._raw = raw_connection

    def execute(self, query, params=None):
        cursor = self._raw.cursor()
        cursor.execute(query, params or ())
        return cursor

    def executemany(self, query, params_list):
        cursor = self._raw.cursor()
        cursor.executemany(query, params_list)
        return cursor

    def execute_values(self, query, values, page_size=1000):
        """대량 삽입 전용. executemany는 행마다 왕복(round trip)이
        발생해 느리지만, 이 메서드는 여러 행을 한 번의 INSERT 문으로
        묶어 보낸다(query에는 "VALUES %s" 형태의 자리표시자를 쓴다)."""

        cursor = self._raw.cursor()
        psycopg2.extras.execute_values(
            cursor, query, values, page_size=page_size
        )
        return cursor

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        _pool.putconn(self._raw)


def get_connection():
    return PooledConnection(_pool.getconn())


def init_db():
    connection = get_connection()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS search_keywords (
            id SERIAL PRIMARY KEY,
            keyword TEXT NOT NULL UNIQUE,
            count INTEGER NOT NULL DEFAULT 1,
            last_collected_at TIMESTAMPTZ
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
            id SERIAL PRIMARY KEY,
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

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_search_keywords (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            keyword TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(username, keyword)
        )
        """
    )

    # 수집기(collector.py)가 채우고, 웹 서버는 조회만 하는 테이블.
    # link에 UNIQUE 제약을 걸어 같은 기사가 여러 번 수집되어도
    # INSERT ... ON CONFLICT DO NOTHING 한 줄로 중복이 걸러지도록 했다.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            link TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            category TEXT NOT NULL,
            published_at TIMESTAMPTZ,
            collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    # 목록/검색 화면이 항상 published_at DESC로 정렬하기 때문에
    # 정렬 기준 컬럼을 인덱스 앞쪽에 둔다.
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_category_published
        ON articles (category, published_at DESC)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_published_at
        ON articles (published_at DESC)
        """
    )

    # 검색(WHERE title ILIKE '%keyword%')은 와일드카드가 앞에 붙어서
    # 일반 B-tree 인덱스로는 가속할 수 없다(Seq Scan으로 빠짐).
    # pg_trgm의 트라이그램 GIN 인덱스는 문자열 중간에 있는 부분
    # 일치도 가속할 수 있어서 ILIKE 패턴을 그대로 쓰면서도 인덱스를
    # 탈 수 있다. (Postgres 기본 전문검색(to_tsvector)은 한글 형태소
    # 분석을 지원하지 않아 이 프로젝트에는 맞지 않는다고 판단했다.)
    connection.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_title_trgm
        ON articles USING GIN (title gin_trgm_ops)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_content_trgm
        ON articles USING GIN (content gin_trgm_ops)
        """
    )

    # 수집 배치가 실행될 때마다 키워드 단위로 실행 결과를 남긴다.
    # 장애가 발생했을 때 "언제, 어떤 키워드에서, 왜" 실패했는지
    # 바로 확인할 수 있게 하기 위한 운영 로그 테이블이다.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_logs (
            id SERIAL PRIMARY KEY,
            keyword TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
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

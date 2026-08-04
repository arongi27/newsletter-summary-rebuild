from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

from urllib.parse import urlparse
import html
import os
import re
import sqlite3

import requests


load_dotenv()

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")


app = Flask(__name__)

# 배포할 때는 .env에 FLASK_SECRET_KEY를 따로 저장하는 것이 안전하다.
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "newshub_development_secret_key",
)


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


def fetch_naver_news(query, display=10, sort="date"):
    """네이버 뉴스 검색 API에서 뉴스 목록을 가져온다."""

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print(
            "NAVER_CLIENT_ID 또는 "
            "NAVER_CLIENT_SECRET이 설정되지 않았습니다."
        )
        return []

    url = "https://naverapihub.apigw.ntruss.com/search/v1/news"

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

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()
        return data.get("items", [])

    except requests.RequestException as error:
        print(f"네이버 뉴스 API 요청 오류: {error}")
        return []
    except ValueError as error:
        print(f"네이버 뉴스 API 응답 처리 오류: {error}")
        return []


def convert_api_news(api_items):
    """네이버 API 응답을 템플릿에서 사용하는 형식으로 변환한다."""

    converted_news = []

    for index, item in enumerate(api_items, start=1):
        original_link = (
            item.get("originallink")
            or item.get("link")
            or ""
        )

        converted_news.append(
            {
                "id": index,
                "title": clean_api_text(
                    item.get("title", "")
                ),
                "content": clean_api_text(
                    item.get("description", "")
                ),
                "date": item.get("pubDate", ""),
                "source": get_news_source(original_link),
                "link": original_link,
            }
        )

    return converted_news


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
    connection = sqlite3.connect("news.db")

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

    connection.commit()
    connection.close()

@app.route("/")
def home():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()

    # 검색창에서 직접 검색한 경우에만 검색 기록을 저장한다.
    if query:
        connection = sqlite3.connect("news.db")

        existing_keyword = connection.execute(
            """
            SELECT id
            FROM search_keywords
            WHERE keyword = ?
            """,
            (query,),
        ).fetchone()

        if existing_keyword:
            connection.execute(
                """
                UPDATE search_keywords
                SET count = count + 1
                WHERE keyword = ?
                """,
                (query,),
            )
        else:
            connection.execute(
                """
                INSERT INTO search_keywords (
                    keyword
                )
                VALUES (?)
                """,
                (query,),
            )

        # 로그인한 사용자의 개인 검색 기록도
        # 직접 검색한 경우에만 저장한다.
        username = session.get("username")

        if username:
            existing_user_keyword = connection.execute(
                """
                SELECT id
                FROM user_search_keywords
                WHERE username = ?
                  AND keyword = ?
                """,
                (username, query),
            ).fetchone()

            if existing_user_keyword:
                connection.execute(
                    """
                    UPDATE user_search_keywords
                    SET count = count + 1
                    WHERE username = ?
                      AND keyword = ?
                    """,
                    (username, query),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO user_search_keywords (
                        username,
                        keyword
                    )
                    VALUES (?, ?)
                    """,
                    (username, query),
                )

        connection.commit()
        connection.close()

    # 실제 API 검색어 결정
    if query:
        api_query = query
    elif category:
        api_query = category
    else:
        api_query = "오늘 뉴스"

    api_items = fetch_naver_news(
        query=api_query,
        display=10,
        sort="date",
    )

    filtered_news = convert_api_news(api_items)

    connection = sqlite3.connect("news.db")

    popular_keywords = connection.execute(
        """
        SELECT keyword, count
        FROM search_keywords
        ORDER BY count DESC, keyword ASC
        LIMIT 5
        """
    ).fetchall()

    connection.close()

    return render_template(
        "index.html",
        news_list=filtered_news,
        query=query,
        category=category,
        popular_keywords=popular_keywords,
    )

@app.route("/autocomplete")
def autocomplete():
    query = request.args.get("q", "").strip()

    if not query:
        return {"keywords": []}

    connection = sqlite3.connect("news.db")

    keywords = connection.execute(
        """
        SELECT keyword
        FROM search_keywords
        WHERE keyword LIKE ?
        ORDER BY count DESC, keyword ASC
        LIMIT 5
        """,
        (f"{query}%",),
    ).fetchall()

    connection.close()

    return {
        "keywords": [
            keyword[0]
            for keyword in keywords
        ]
    }


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            return "아이디와 비밀번호를 입력해 주세요.", 400

        hashed_password = generate_password_hash(password)

        connection = sqlite3.connect("news.db")

        existing_user = connection.execute(
            """
            SELECT id
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

        if existing_user:
            connection.close()
            return "이미 존재하는 아이디입니다."

        connection.execute(
            """
            INSERT INTO users (
                username,
                password
            )
            VALUES (?, ?)
            """,
            (username, hashed_password),
        )

        connection.commit()
        connection.close()

        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        connection = sqlite3.connect("news.db")

        user = connection.execute(
            """
            SELECT id, username, password
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

        connection.close()

        if user and check_password_hash(
            user[2],
            password,
        ):
            session["username"] = username
            return redirect(url_for("home"))

        return "아이디 또는 비밀번호가 올바르지 않습니다."

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("home"))


@app.route(
    "/favorite/add/<int:news_id>",
    methods=["POST"],
)
def add_favorite(news_id):
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    news_title = request.form.get(
        "news_title",
        "",
    ).strip()

    news_content = request.form.get(
        "news_content",
        "",
    ).strip()

    news_date = request.form.get(
        "news_date",
        "",
    ).strip()

    news_source = request.form.get(
        "news_source",
        "네이버 뉴스",
    ).strip()

    news_link = request.form.get(
        "news_link",
        "",
    ).strip()

    if not news_title or not news_link:
        return "즐겨찾기할 뉴스 정보가 없습니다.", 400

    connection = sqlite3.connect("news.db")

    existing_favorite = connection.execute(
        """
        SELECT id
        FROM favorites
        WHERE username = ?
          AND news_link = ?
        """,
        (username, news_link),
    ).fetchone()

    if not existing_favorite:
        connection.execute(
            """
            INSERT INTO favorites (
                username,
                news_title,
                news_content,
                news_date,
                news_source,
                news_link
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                news_title,
                news_content,
                news_date,
                news_source,
                news_link,
            ),
        )

        connection.commit()

    connection.close()

    return redirect(
        request.referrer
        or url_for("home")
    )


@app.route("/favorites")
def favorites():
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    connection = sqlite3.connect("news.db")

    favorite_news = connection.execute(
        """
        SELECT
            id,
            news_title,
            news_content,
            news_date,
            news_source,
            news_link
        FROM favorites
        WHERE username = ?
        ORDER BY id DESC
        """,
        (username,),
    ).fetchall()

    connection.close()

    return render_template(
        "favorites.html",
        favorite_news=favorite_news,
    )


@app.route(
    "/favorite/delete/<int:favorite_id>",
    methods=["POST"],
)
def delete_favorite(favorite_id):
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    connection = sqlite3.connect("news.db")

    connection.execute(
        """
        DELETE FROM favorites
        WHERE id = ?
          AND username = ?
        """,
        (favorite_id, username),
    )

    connection.commit()
    connection.close()

    return redirect(url_for("favorites"))


@app.route("/interests")
def interests():
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    connection = sqlite3.connect("news.db")

    keywords = connection.execute(
        """
        SELECT keyword, count
        FROM user_search_keywords
        WHERE username = ?
        ORDER BY count DESC, keyword ASC
        LIMIT 5
        """,
        (username,),
    ).fetchall()

    connection.close()

    recommended_news = []
    saved_links = set()

    # 상위 검색어별로 실제 뉴스를 가져와 추천한다.
    for keyword, count in keywords:
        api_items = fetch_naver_news(
            query=keyword,
            display=5,
            sort="date",
        )

        converted_items = convert_api_news(api_items)

        for news in converted_items:
            news_link = news.get("link", "")

            if not news_link:
                continue

            if news_link in saved_links:
                continue

            saved_links.add(news_link)
            recommended_news.append(news)

            if len(recommended_news) >= 10:
                break

        if len(recommended_news) >= 10:
            break

    return render_template(
        "interests.html",
        keywords=keywords,
        recommended_news=recommended_news,
    )


@app.route("/profile")
def profile():
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    connection = sqlite3.connect("news.db")

    favorite_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM favorites
        WHERE username = ?
        """,
        (username,),
    ).fetchone()[0]

    search_count = connection.execute(
        """
        SELECT COALESCE(
            SUM(count),
            0
        )
        FROM user_search_keywords
        WHERE username = ?
        """,
        (username,),
    ).fetchone()[0]

    top_keyword_row = connection.execute(
        """
        SELECT keyword, count
        FROM user_search_keywords
        WHERE username = ?
        ORDER BY count DESC, keyword ASC
        LIMIT 1
        """,
        (username,),
    ).fetchone()

    connection.close()

    if top_keyword_row:
        top_keyword = top_keyword_row[0]
        top_keyword_count = top_keyword_row[1]
    else:
        top_keyword = "아직 없음"
        top_keyword_count = 0

    return render_template(
        "profile.html",
        username=username,
        favorite_count=favorite_count,
        search_count=search_count,
        top_keyword=top_keyword,
        top_keyword_count=top_keyword_count,
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
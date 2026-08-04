from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3


app = Flask(__name__)
app.secret_key = "newshub_secret_key"


def init_db():
    connection = sqlite3.connect("news.db")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
    """)

    # 전체 사용자의 인기 검색어를 저장하는 테이블
    connection.execute("""
        CREATE TABLE IF NOT EXISTS search_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            count INTEGER NOT NULL DEFAULT 1
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            news_id INTEGER NOT NULL,
            news_title TEXT NOT NULL,
            news_content TEXT NOT NULL,
            news_date TEXT NOT NULL,
            news_source TEXT NOT NULL,
            UNIQUE(username, news_id)
        )
    """)

    # 로그인 사용자별 검색 기록을 저장하는 테이블
    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_search_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            keyword TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(username, keyword)
        )
    """)

    connection.commit()
    connection.close()


news_list = [
    {
        "id": 1,
        "title": "🔥 ChatGPT가 자동으로 만든 첫 번째 뉴스입니다.",
        "content": "첫 번째 테스트 뉴스입니다.",
        "date": "2026-08-02",
        "source": "NewsHub"
    },
    {
        "id": 2,
        "title": "공공기관의 디지털 전환이 본격적으로 추진되고 있습니다.",
        "content": "두 번째 테스트 뉴스입니다.",
        "date": "2026-08-02",
        "source": "NewsHub"
    },
    {
        "id": 3,
        "title": "반도체 시장의 성장세가 이어지고 있습니다.",
        "content": "세 번째 테스트 뉴스입니다.",
        "date": "2026-08-02",
        "source": "NewsHub"
    }
]


@app.route("/")
def home():
    query = request.args.get("q", "").strip()

    if query:
        connection = sqlite3.connect("news.db")

        # 전체 인기 검색어 집계
        existing_keyword = connection.execute(
            """
            SELECT id
            FROM search_keywords
            WHERE keyword = ?
            """,
            (query,)
        ).fetchone()

        if existing_keyword:
            connection.execute(
                """
                UPDATE search_keywords
                SET count = count + 1
                WHERE keyword = ?
                """,
                (query,)
            )
        else:
            connection.execute(
                """
                INSERT INTO search_keywords (keyword)
                VALUES (?)
                """,
                (query,)
            )

        # 로그인한 사용자의 개인 검색 기록 집계
        username = session.get("username")

        if username:
            existing_user_keyword = connection.execute(
                """
                SELECT id
                FROM user_search_keywords
                WHERE username = ? AND keyword = ?
                """,
                (username, query)
            ).fetchone()

            if existing_user_keyword:
                connection.execute(
                    """
                    UPDATE user_search_keywords
                    SET count = count + 1
                    WHERE username = ? AND keyword = ?
                    """,
                    (username, query)
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
                    (username, query)
                )

        connection.commit()
        connection.close()

        filtered_news = [
            news for news in news_list
            if query.lower() in news["title"].lower()
            or query.lower() in news["content"].lower()
        ]

    else:
        filtered_news = news_list

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
        popular_keywords=popular_keywords
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
        (f"{query}%",)
    ).fetchall()

    connection.close()

    return {
        "keywords": [keyword[0] for keyword in keywords]
    }


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        hashed_password = generate_password_hash(password)

        connection = sqlite3.connect("news.db")

        existing_user = connection.execute(
            """
            SELECT id
            FROM users
            WHERE username = ?
            """,
            (username,)
        ).fetchone()

        if existing_user:
            connection.close()
            return "이미 존재하는 아이디입니다."

        connection.execute(
            """
            INSERT INTO users (username, password)
            VALUES (?, ?)
            """,
            (username, hashed_password)
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
            (username,)
        ).fetchone()

        connection.close()

        if user and check_password_hash(user[2], password):
            session["username"] = username
            return redirect(url_for("home"))

        return "아이디 또는 비밀번호가 올바르지 않습니다."

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("home"))


@app.route("/favorite/add/<int:news_id>", methods=["POST"])
def add_favorite(news_id):
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    selected_news = next(
        (news for news in news_list if news["id"] == news_id),
        None
    )

    if selected_news is None:
        return "존재하지 않는 뉴스입니다.", 404

    connection = sqlite3.connect("news.db")

    existing_favorite = connection.execute(
        """
        SELECT id
        FROM favorites
        WHERE username = ? AND news_id = ?
        """,
        (username, news_id)
    ).fetchone()

    if not existing_favorite:
        connection.execute(
            """
            INSERT INTO favorites (
                username,
                news_id,
                news_title,
                news_content,
                news_date,
                news_source
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                selected_news["id"],
                selected_news["title"],
                selected_news["content"],
                selected_news["date"],
                selected_news["source"]
            )
        )

        connection.commit()

    connection.close()

    return redirect(request.referrer or url_for("home"))


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
            news_source
        FROM favorites
        WHERE username = ?
        ORDER BY id DESC
        """,
        (username,)
    ).fetchall()

    connection.close()

    return render_template(
        "favorites.html",
        favorite_news=favorite_news
    )


@app.route("/favorite/delete/<int:favorite_id>", methods=["POST"])
def delete_favorite(favorite_id):
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    connection = sqlite3.connect("news.db")

    connection.execute(
        """
        DELETE FROM favorites
        WHERE id = ? AND username = ?
        """,
        (favorite_id, username)
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

    # 현재 로그인한 사용자가 가장 많이 검색한 키워드 5개
    keywords = connection.execute(
        """
        SELECT keyword, count
        FROM user_search_keywords
        WHERE username = ?
        ORDER BY count DESC, keyword ASC
        LIMIT 5
        """,
        (username,)
    ).fetchall()

    connection.close()

    keyword_list = [row[0] for row in keywords]
    recommended_news = []

    # 주요 검색어와 제목 또는 내용이 일치하는 뉴스 추천
    for news in news_list:
        for keyword in keyword_list:
            if (
                keyword.lower() in news["title"].lower()
                or keyword.lower() in news["content"].lower()
            ):
                recommended_news.append(news)
                break

    return render_template(
        "interests.html",
        keywords=keywords,
        recommended_news=recommended_news
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
        (username,)
    ).fetchone()[0]

    search_count = connection.execute(
        """
        SELECT COALESCE(SUM(count), 0)
        FROM user_search_keywords
        WHERE username = ?
        """,
        (username,)
    ).fetchone()[0]

    top_keyword_row = connection.execute(
        """
        SELECT keyword, count
        FROM user_search_keywords
        WHERE username = ?
        ORDER BY count DESC, keyword ASC
        LIMIT 1
        """,
        (username,)
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
        top_keyword_count=top_keyword_count
    )

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
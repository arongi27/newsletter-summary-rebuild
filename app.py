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

    connection.execute("""
        CREATE TABLE IF NOT EXISTS search_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            count INTEGER NOT NULL DEFAULT 1
        )
    """)

    connection.close()


news_list = [
    {
        "title": "🔥 ChatGPT가 자동으로 만든 첫 번째 뉴스입니다.",
        "content": "첫 번째 테스트 뉴스입니다.",
        "date": "2026-08-02",
        "source": "NewsHub"
    },
    {
        "title": "공공기관의 디지털 전환이 본격적으로 추진되고 있습니다.",
        "content": "두 번째 테스트 뉴스입니다.",
        "date": "2026-08-02",
        "source": "NewsHub"
    },
    {
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

        existing_keyword = connection.execute(
            "SELECT * FROM search_keywords WHERE keyword = ?",
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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        hashed_password = generate_password_hash(password)

        connection = sqlite3.connect("news.db")

        existing_user = connection.execute(
            "SELECT * FROM users WHERE username = ?",
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

        return "회원가입이 완료되었습니다!"

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        connection = sqlite3.connect("news.db")

        user = connection.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        connection.close()

        if user and check_password_hash(user[2], password):
            session["username"] = username
            return redirect(url_for("home"))

        return "아이디 또는 비밀번호가 올바르지 않습니다."

    return render_template("login.html")
@app.route("/autocomplete")
def autocomplete():
    query = request.args.get("q", "").strip()

    connection = sqlite3.connect("news.db")

    keywords = connection.execute(
        """
        SELECT keyword
        FROM search_keywords
        WHERE keyword LIKE ?
        ORDER BY count DESC
        LIMIT 5
        """,
        (f"{query}%",)
    ).fetchall()

    connection.close()

    return {
        "keywords": [k[0] for k in keywords]
    }

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
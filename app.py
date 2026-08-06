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
from collections import Counter
from wordcloud import WordCloud
from kiwipiepy import Kiwi

import html
import os
import re
import sqlite3
import requests
import networkx as nx
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib import font_manager

load_dotenv()

kiwi = Kiwi()

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

def normalize_keyword(word):
    normalized = str(word)

    normalized = re.sub(
        r"[^가-힣a-zA-Z0-9]",
        "",
        normalized,
    )

    return normalized

def get_stopwords():
    """워드클라우드와 관계도에서 제외할 일반적인 단어 목록을 반환한다."""

    return {
                # 기본 불용어
        "기자", "뉴스", "오늘", "관련", "통해",
        "대한", "이번", "지난", "위해", "가운데",
        "따르면", "대해", "이날", "최대", "최근",
        "전국", "프로젝트", "명칭", "발표", "진행",

        # 일반 명사
        "정부", "기업", "시장", "산업", "분야",
        "경제", "지역", "사업", "교육", "지원",
        "계획", "시스템", "센터", "데이터", "기술",
        "국내", "업종", "자산", "운용", "참여",

        # 기사에서 자주 등장하지만 의미가 약한 단어
        "상승", "하락", "강세", "약세",
        "증가", "감소", "확대", "축소",
        "개최", "운영", "추진", "제공",
        "설명", "예정", "발생", "가능",
        "이용", "활용", "구축", "도입",
        "관계자", "대표", "업계", "모든",
        "모두", "이상", "이하", "경우",

         # 실적 기사에서 자주 나오는 단어
        "분기", "상반기", "하반기",
        "전년", "대비", "이익",
        "영업", "실적",

        # 지역/기관명(관계도 품질 향상용)
        "서울", "경기", "인천", "부산",
        "대구", "광주", "대전", "울산",
    }


def extract_keywords_from_text(text):
    """한 문장에서 명사형 핵심 키워드만 추출한다."""

    stopwords = get_stopwords()
    keywords = []

    for token in kiwi.tokenize(text or ""):
        if not token.tag.startswith("N"):
            continue

        word = normalize_keyword(token.form)

        if len(word) < 2:
            continue

        if word in stopwords:
            continue

        if word.isdigit():
            continue

        keywords.append(word)

    return keywords


def extract_keywords(news_list):
    """현재 뉴스 목록 전체의 키워드 등장 횟수를 계산한다."""

    text_parts = []

    for news in news_list:
        text_parts.append(news.get("title", ""))
        text_parts.append(news.get("content", ""))

    full_text = " ".join(text_parts)
    return Counter(extract_keywords_from_text(full_text))

def build_keyword_graph(news_list, max_keywords=10):
    overall_counts = extract_keywords(news_list)

    top_keywords = {
        normalize_keyword(word)
        for word, _ in overall_counts.most_common(max_keywords)
    }

    graph = nx.Graph()

    for word in top_keywords:
        graph.add_node(
            word,
            count=overall_counts[word],
        )

    for news in news_list:
        article_text = (
            news["title"] + " " + news["content"]
        )

        article_keywords = set()

        for token in kiwi.tokenize(article_text):
            if not token.tag.startswith("N"):
                continue

            word = normalize_keyword(token.form)

            if word in top_keywords:
                article_keywords.add(word)

        article_keywords = sorted(article_keywords)

        for index, first_word in enumerate(article_keywords):
            for second_word in article_keywords[index + 1:]:
                if graph.has_edge(first_word, second_word):
                    graph[first_word][second_word]["weight"] += 1
                else:
                    graph.add_edge(
                        first_word,
                        second_word,
                        weight=1,
                    )

    return graph

def generate_keyword_graph_image(news_list, focus_keyword=""):
    graph = build_keyword_graph(
        news_list,
        max_keywords=10,
    )

    if graph.number_of_nodes() == 0:
        return None

    # 혹시 남아 있는 공백이나 특수문자를
    # 이미지 생성 직전에 한 번 더 정리한다.
    normalized_graph = nx.Graph()

    for node, data in graph.nodes(data=True):
        normalized_node = normalize_keyword(node)

        if not normalized_node:
            continue

        current_count = normalized_graph.nodes.get(
            normalized_node,
            {},
        ).get("count", 0)

        normalized_graph.add_node(
            normalized_node,
            count=max(
                current_count,
                data.get("count", 1),
            ),
        )

    for first_word, second_word, data in graph.edges(data=True):
        normalized_first = normalize_keyword(first_word)
        normalized_second = normalize_keyword(second_word)

        if (
            not normalized_first
            or not normalized_second
            or normalized_first == normalized_second
        ):
            continue

        edge_weight = data.get("weight", 1)

        if normalized_graph.has_edge(
            normalized_first,
            normalized_second,
        ):
            normalized_graph[
                normalized_first
            ][
                normalized_second
            ]["weight"] += edge_weight
        else:
            normalized_graph.add_edge(
                normalized_first,
                normalized_second,
                weight=edge_weight,
            )

    if normalized_graph.number_of_nodes() == 0:
        return None

    output_filename = "keyword_graph.png"
    output_path = os.path.join("static", output_filename)

    font_path = "C:/Windows/Fonts/malgun.ttf"
    font_manager.fontManager.addfont(font_path)

    font_name = font_manager.FontProperties(
        fname=font_path,
    ).get_name()

    plt.figure(figsize=(14, 8))

    normalized_focus = normalize_keyword(focus_keyword)

    if normalized_focus in normalized_graph:
        positions = nx.spring_layout(
            normalized_graph,
            seed=42,
            k=0.8,
            pos={normalized_focus: (0, 0)},
            fixed=[normalized_focus],
        )
    else:
        positions = nx.spring_layout(
            normalized_graph,
            seed=42,
            k=0.8,
        )

    # 등장 횟수와 글자 수를 함께 반영해
    # 긴 키워드도 원 안에서 잘 보이도록 한다.
    node_sizes = [
        1400
        + normalized_graph.nodes[node].get("count", 1) * 220
        + min(len(node), 8) * 160
        for node in normalized_graph.nodes
    ]

    edge_widths = [
        0.8
        + normalized_graph[first_word][second_word].get(
            "weight",
            1,
        ) * 0.8
        for first_word, second_word in normalized_graph.edges
    ]

    node_colors = [
        "#4fa8dc"
        if node == normalized_focus
        else "#a9d8ef"
        for node in normalized_graph.nodes
    ]

    node_borders = [
        2.8
        if node == normalized_focus
        else 1.4
        for node in normalized_graph.nodes
    ]

    nx.draw_networkx_nodes(
        normalized_graph,
        positions,
        node_size=node_sizes,
        node_color=node_colors,
        edgecolors="#438fbd",
        linewidths=node_borders,
        alpha=0.95,
    )

    nx.draw_networkx_edges(
        normalized_graph,
        positions,
        width=edge_widths,
        edge_color="#a8bfd3",
        alpha=0.7,
    )

    nx.draw_networkx_labels(
        normalized_graph,
        positions,
        font_family=font_name,
        font_size=13,
        font_color="#123b63",
    )

    plt.axis("off")
    plt.margins(0.08)
    plt.tight_layout()

    plt.savefig(
        output_path,
        dpi=180,
        bbox_inches="tight",
        pad_inches=0.15,
        facecolor="white",
    )

    plt.close()

    return output_filename


def generate_wordcloud(news_list):
    keyword_counts = extract_keywords(news_list)

    if not keyword_counts:
        return None

    output_path = os.path.join(
        "static",
        "wordcloud.png",
    )

    wordcloud = WordCloud(
        font_path="C:/Windows/Fonts/malgun.ttf",
        width=900,
        height=450,
        background_color="white",
        max_words=50,
        colormap="Blues",
    ).generate_from_frequencies(keyword_counts)

    wordcloud.to_file(output_path)

    return "wordcloud.png"

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

    wordcloud_filename = generate_wordcloud(filtered_news)

    keyword_graph = generate_keyword_graph_image(
        filtered_news,
        focus_keyword=api_query,
    )

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
        wordcloud_filename=wordcloud_filename,
        keyword_graph=keyword_graph,
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

    if keywords:
        max_count = keywords[0][1]

        interest_bars = [
            {
                "keyword": keyword,
                "percentage": round(
                    count / max_count * 100
                ),
            }
            for keyword, count in keywords
        ]
    else:
        interest_bars = []
    
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
        interest_bars=interest_bars,
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
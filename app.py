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
from collections import Counter
from urllib.parse import urlparse

import base64
import hmac
import io
import os
import re
import secrets

import db

load_dotenv()

kiwi = None
IS_RENDER = bool(os.getenv("RENDER"))
ENABLE_VISUALIZATIONS = os.getenv(
    "ENABLE_VISUALIZATIONS",
    "false" if IS_RENDER else "true",
).lower() == "true"

ARTICLE_LIST_LIMIT = 10

# 회원가입 입력 규칙. 기존 사용자의 로그인에는 적용하지 않는다.
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{4,20}$")
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

# OS에 내장된 폰트 경로에 의존하면 배포 환경(Render)에는 한글 폰트가
# 없어서 워드클라우드/관계도의 한글이 깨진다. 라이선스상 재배포 가능한
# (SIL OFL) 나눔고딕을 static/fonts/에 직접 번들해 로컬/배포 어디서든
# 동일하게 동작하게 했다.
KOREAN_FONT_PATH = os.path.join("static", "fonts", "NanumGothic-Regular.ttf")


def get_korean_font_path():
    if os.path.exists(KOREAN_FONT_PATH):
        return KOREAN_FONT_PATH

    return None


app = Flask(__name__)

# secret_key는 세션 쿠키 서명에 쓰인다. 예전에는 환경변수가 없으면
# 공개 저장소에 노출된 기본값으로 조용히 동작했다. db.py의 DATABASE_URL
# 처리와 같은 원칙으로, 운영 환경(Render)에서는 키가 없으면 시작 자체를
# 거부한다. 로컬 개발에서만 임시 값을 허용한다.
secret_key = os.getenv("FLASK_SECRET_KEY")

if not secret_key:
    if IS_RENDER:
        raise RuntimeError(
            "운영 환경에서는 FLASK_SECRET_KEY 환경변수가 반드시 필요합니다."
        )

    secret_key = "local-development-only"

app.secret_key = secret_key

# 세션 쿠키 보안 속성.
# - SameSite=Lax: 다른 사이트에서 보낸 POST 요청에는 세션 쿠키를 붙이지
#   않는다(CSRF 1차 방어). 브라우저 기본값에 기대지 않고 명시한다.
# - Secure: HTTPS에서만 쿠키를 전송한다. 로컬(http)에서는 끈다.
# - HttpOnly: 자바스크립트에서 쿠키를 읽지 못하게 한다(Flask 기본값).
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_RENDER,
    SESSION_COOKIE_HTTPONLY=True,
)


def get_csrf_token():
    """세션마다 하나의 CSRF 토큰을 만들어 폼에 넣는다."""

    token = session.get("csrf_token")

    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token

    return token


app.jinja_env.globals["csrf_token"] = get_csrf_token


@app.before_request
def check_csrf_token():
    """상태를 바꾸는 모든 POST 요청에서 CSRF 토큰을 검사한다.

    다른 사이트는 사용자의 세션 쿠키를 브라우저가 자동으로 붙이게 만들 수는
    있어도, 우리 페이지 안에 들어 있는 토큰 값은 읽을 수 없다. 폼에 숨겨
    보낸 토큰이 세션의 토큰과 같을 때만 요청을 처리한다. 비교는 시간 차이로
    값이 드러나지 않도록 hmac.compare_digest를 쓴다.
    """

    if request.method != "POST":
        return None

    sent_token = request.form.get("csrf_token", "")
    expected_token = session.get("csrf_token", "")

    if not expected_token or not hmac.compare_digest(sent_token, expected_token):
        return "요청이 만료되었습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.", 400

    return None


def redirect_back(default_endpoint="home"):
    """직전 페이지로 돌아가되, 같은 사이트 주소일 때만 허용한다.

    Referer 헤더는 요청을 보내는 쪽이 정하는 값이라 외부 주소가 들어올 수
    있다. 호스트가 현재 사이트와 같을 때만 따라가고, 아니면 기본 페이지로
    보낸다.
    """

    referrer = request.referrer

    if referrer and urlparse(referrer).netloc == request.host:
        return redirect(referrer)

    return redirect(url_for(default_endpoint))


def escape_like(text):
    """LIKE 패턴에서 특수 의미를 갖는 문자를 글자 그대로 검색되게 바꾼다.

    사용자가 %나 _를 입력하면 와일드카드로 해석되어 '%' 한 글자 검색이
    모든 기사와 일치해 버린다. 쿼리에 ESCAPE '\\'를 명시하고, 백슬래시를
    먼저 바꾼 뒤 %와 _ 앞에 붙인다.
    """

    return (
        text.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def format_published_at(value):
    """TIMESTAMPTZ로 조회된 datetime을 화면 표시용 문자열로 바꾼다."""

    if not value:
        return ""

    return value.strftime("%Y-%m-%d %H:%M")


def rows_to_news_list(rows):
    """articles 테이블 조회 결과를 템플릿에서 쓰는 형식으로 변환한다."""

    return [
        {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "date": format_published_at(row[3]),
            "source": row[4],
            "link": row[5],
        }
        for row in rows
    ]


def search_articles(connection, query, limit=ARTICLE_LIST_LIMIT):
    pattern = f"%{escape_like(query)}%"

    rows = connection.execute(
        """
        SELECT id, title, content, published_at, source, link
        FROM articles
        WHERE title ILIKE %s ESCAPE '\\' OR content ILIKE %s ESCAPE '\\'
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s
        """,
        (pattern, pattern, limit),
    ).fetchall()

    return rows_to_news_list(rows)


def list_articles_by_category(connection, category, limit=ARTICLE_LIST_LIMIT):
    rows = connection.execute(
        """
        SELECT id, title, content, published_at, source, link
        FROM articles
        WHERE category = %s
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s
        """,
        (category, limit),
    ).fetchall()

    return rows_to_news_list(rows)


def list_recent_articles(connection, limit=ARTICLE_LIST_LIMIT):
    rows = connection.execute(
        """
        SELECT id, title, content, published_at, source, link
        FROM articles
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    ).fetchall()

    return rows_to_news_list(rows)


def record_search(connection, query, username):
    """검색어 횟수를 전체/개인 기록에 반영한다.

    예전에는 SELECT로 먼저 있는지 확인한 뒤 UPDATE 또는 INSERT를 했다.
    이 방식은 같은 새 검색어가 동시에 들어오면 두 요청이 모두 "없음"을
    보고 INSERT를 시도해, 한쪽이 UNIQUE 제약 위반 오류를 받는다.
    collector.py가 ON CONFLICT로 중복 판단을 DB 제약에 맡기는 것과 같은
    원칙으로, 존재 여부 확인과 증가를 upsert 한 문장으로 처리한다.
    """

    connection.execute(
        """
        INSERT INTO search_keywords (keyword)
        VALUES (%s)
        ON CONFLICT (keyword)
        DO UPDATE SET count = search_keywords.count + 1
        """,
        (query,),
    )

    if username:
        connection.execute(
            """
            INSERT INTO user_search_keywords (username, keyword)
            VALUES (%s, %s)
            ON CONFLICT (username, keyword)
            DO UPDATE SET count = user_search_keywords.count + 1
            """,
            (username, query),
        )


def is_waiting_for_collection(connection, query):
    """검색어가 아직 한 번도 수집되지 않았는지 확인한다.

    결과가 0건일 때 "아직 수집 안 된 키워드"와 "수집했지만 기사가 없는
    키워드"를 구분하기 위해 search_keywords.last_collected_at을 본다.
    """

    row = connection.execute(
        """
        SELECT last_collected_at
        FROM search_keywords
        WHERE keyword = %s
        """,
        (query,),
    ).fetchone()

    return row is None or row[0] is None


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


def get_kiwi():
    """형태소 분석기는 실제로 필요할 때 한 번만 생성한다."""

    global kiwi

    if kiwi is None:
        from kiwipiepy import Kiwi

        kiwi = Kiwi()

    return kiwi


def extract_keywords_from_text(text):
    """한 문장에서 명사형 핵심 키워드만 추출한다."""

    stopwords = get_stopwords()
    keywords = []

    for token in get_kiwi().tokenize(text or ""):
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
    import networkx as nx

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

        for token in get_kiwi().tokenize(article_text):
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
    import networkx as nx
    import matplotlib

    matplotlib.use("Agg")

    import matplotlib.pyplot as plt
    from matplotlib import font_manager

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

    font_path = get_korean_font_path()

    if font_path:
        font_manager.fontManager.addfont(font_path)
        font_name = font_manager.FontProperties(
            fname=font_path,
        ).get_name()
    else:
        font_name = "DejaVu Sans"

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

    # 고정 파일(static/keyword_graph.png)에 저장하면 동시에 들어온 요청이
    # 서로의 이미지를 덮어쓴다. 메모리에서 PNG를 만들어 data URI로 넘긴다.
    buffer = io.BytesIO()

    plt.savefig(
        buffer,
        format="png",
        dpi=180,
        bbox_inches="tight",
        pad_inches=0.15,
        facecolor="white",
    )

    plt.close()

    return png_bytes_to_data_uri(buffer.getvalue())


def png_bytes_to_data_uri(png_bytes):
    """PNG 바이트를 <img src="...">에 바로 넣을 수 있는 data URI로 바꾼다."""

    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def generate_wordcloud(news_list):
    from wordcloud import WordCloud

    keyword_counts = extract_keywords(news_list)

    if not keyword_counts:
        return None

    font_path = get_korean_font_path()

    wordcloud = WordCloud(
        font_path=font_path,
        width=900,
        height=450,
        background_color="white",
        max_words=50,
        colormap="Blues",
    ).generate_from_frequencies(keyword_counts)

    buffer = io.BytesIO()
    wordcloud.to_image().save(buffer, format="PNG")

    return png_bytes_to_data_uri(buffer.getvalue())


@app.route("/")
def home():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()

    waiting_for_collection = False

    with db.connection() as connection:
        # q 파라미터로 들어온 검색을 기록한다. 검색창 제출뿐 아니라
        # 인기 검색어·추천 키워드 링크를 눌러 들어온 경우도 포함된다.
        if query:
            record_search(connection, query, session.get("username"))
            connection.commit()

        # 웹 서버는 외부 API를 호출하지 않고, collector.py가 미리
        # 적재해 둔 articles 테이블을 조회한다.
        if query:
            filtered_news = search_articles(connection, query)
        elif category:
            filtered_news = list_articles_by_category(connection, category)
        else:
            filtered_news = list_recent_articles(connection)

        if query and not filtered_news:
            waiting_for_collection = is_waiting_for_collection(
                connection,
                query,
            )

        popular_keywords = connection.execute(
            """
            SELECT keyword, count
            FROM search_keywords
            ORDER BY count DESC, keyword ASC
            LIMIT 5
            """
        ).fetchall()

    # 시각화는 DB가 필요 없으므로 커넥션을 반납한 뒤에 수행한다.
    if ENABLE_VISUALIZATIONS:
        wordcloud_image = generate_wordcloud(filtered_news)
        keyword_graph_image = generate_keyword_graph_image(
            filtered_news,
            focus_keyword=query or category,
        )
    else:
        wordcloud_image = None
        keyword_graph_image = None

    return render_template(
        "index.html",
        news_list=filtered_news,
        query=query,
        category=category,
        popular_keywords=popular_keywords,
        wordcloud_image=wordcloud_image,
        keyword_graph_image=keyword_graph_image,
        waiting_for_collection=waiting_for_collection,
    )


@app.route("/autocomplete")
def autocomplete():
    query = request.args.get("q", "").strip()

    if not query:
        return {"keywords": []}

    with db.connection() as connection:
        keywords = connection.execute(
            """
            SELECT keyword
            FROM search_keywords
            WHERE keyword LIKE %s ESCAPE '\\'
            ORDER BY count DESC, keyword ASC
            LIMIT 5
            """,
            (f"{escape_like(query)}%",),
        ).fetchall()

    return {
        "keywords": [
            keyword[0]
            for keyword in keywords
        ]
    }


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # 브라우저의 required 속성은 쉽게 우회되므로 서버에서 다시 검사한다.
        if not USERNAME_PATTERN.fullmatch(username):
            return render_template(
                "signup.html",
                error="아이디는 영문, 숫자, 밑줄(_)로 4~20자여야 합니다.",
                username=username,
            ), 400

        if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
            return render_template(
                "signup.html",
                error=f"비밀번호는 {PASSWORD_MIN_LENGTH}자 이상이어야 합니다.",
                username=username,
            ), 400

        hashed_password = generate_password_hash(password)

        # SELECT로 중복을 먼저 확인하면 같은 아이디로 동시에 가입할 때
        # 둘 다 통과한 뒤 한쪽이 UNIQUE 위반 오류를 받는다. 중복 판단을
        # users.username UNIQUE 제약에 맡기고, 삽입된 행 수로 결과를 본다.
        with db.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users (username, password)
                VALUES (%s, %s)
                ON CONFLICT (username) DO NOTHING
                """,
                (username, hashed_password),
            )

            connection.commit()
            created = cursor.rowcount == 1

        if not created:
            return render_template(
                "signup.html",
                error="이미 사용 중인 아이디입니다.",
                username=username,
            ), 409

        return redirect(url_for("login", joined=1))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with db.connection() as connection:
            user = connection.execute(
                """
                SELECT id, username, password
                FROM users
                WHERE username = %s
                """,
                (username,),
            ).fetchone()

        if user and check_password_hash(
            user[2],
            password,
        ):
            session["username"] = username
            return redirect(url_for("home"))

        # 아이디가 없는 경우와 비밀번호가 틀린 경우를 같은 문구로 알려서
        # 어떤 아이디가 가입되어 있는지 추측할 수 없게 한다.
        return render_template(
            "login.html",
            error="아이디 또는 비밀번호가 올바르지 않습니다.",
            username=username,
        ), 401

    return render_template(
        "login.html",
        joined=request.args.get("joined") == "1",
    )


# 로그아웃은 상태를 바꾸는 요청이라 POST로만 받는다. GET이면 다른 사이트에
# <img src="/logout"> 한 줄만 넣어도 방문자를 강제로 로그아웃시킬 수 있다.
@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route(
    "/favorite/add/<int:news_id>",
    methods=["POST"],
)
def add_favorite(news_id):
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    with db.connection() as connection:
        # 예전에는 브라우저가 hidden 필드로 보낸 제목·본문·링크를 그대로
        # 저장했다. 클라이언트가 보낸 값은 조작될 수 있으므로(가짜 제목,
        # javascript: 링크 등), URL의 기사 ID로 서버의 articles에서 직접
        # 조회한 값만 저장한다.
        article = connection.execute(
            """
            SELECT title, content, published_at, source, link
            FROM articles
            WHERE id = %s
            """,
            (news_id,),
        ).fetchone()

        if not article:
            return "존재하지 않는 기사입니다.", 404

        title, content, published_at, source, link = article

        if not link.startswith(("http://", "https://")):
            return "즐겨찾기할 수 없는 기사입니다.", 400

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
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (username, news_link) DO NOTHING
            """,
            (
                username,
                title,
                content,
                format_published_at(published_at),
                source,
                link,
            ),
        )

        connection.commit()

    return redirect_back()


@app.route("/favorites")
def favorites():
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    with db.connection() as connection:
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
            WHERE username = %s
            ORDER BY id DESC
            """,
            (username,),
        ).fetchall()

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

    with db.connection() as connection:
        connection.execute(
            """
            DELETE FROM favorites
            WHERE id = %s
              AND username = %s
            """,
            (favorite_id, username),
        )

        connection.commit()

    return redirect(url_for("favorites"))


@app.route("/interests")
def interests():
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    with db.connection() as connection:
        keywords = connection.execute(
            """
            SELECT keyword, count
            FROM user_search_keywords
            WHERE username = %s
            ORDER BY count DESC, keyword ASC
            LIMIT 5
            """,
            (username,),
        ).fetchall()

        recommended_news = []
        saved_links = set()

        # 상위 검색어별로 DB에 이미 수집된 기사 중에서 추천한다.
        for keyword, count in keywords:
            for news in search_articles(connection, keyword, limit=5):
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

    return render_template(
        "interests.html",
        keywords=keywords,
        interest_bars=interest_bars,
        recommended_news=recommended_news,
    )


@app.route("/search-history/delete", methods=["POST"])
def delete_search_history():
    """내 검색 기록(관심 키워드 분석 데이터)을 모두 삭제한다.

    개인 검색 기록은 관심사가 드러나는 정보라 사용자가 직접 지울 수 있게
    했다. 전체 인기 검색어(search_keywords)는 사용자를 식별하지 않는
    집계값이라 함께 지우지 않는다.
    """

    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    with db.connection() as connection:
        connection.execute(
            """
            DELETE FROM user_search_keywords
            WHERE username = %s
            """,
            (username,),
        )

        connection.commit()

    return redirect(url_for("profile"))


@app.route("/profile")
def profile():
    username = session.get("username")

    if not username:
        return redirect(url_for("login"))

    with db.connection() as connection:
        favorite_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM favorites
            WHERE username = %s
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
            WHERE username = %s
            """,
            (username,),
        ).fetchone()[0]

        top_keyword_row = connection.execute(
            """
            SELECT keyword, count
            FROM user_search_keywords
            WHERE username = %s
            ORDER BY count DESC, keyword ASC
            LIMIT 1
            """,
            (username,),
        ).fetchone()

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
    db.init_db()
    app.run(debug=True)

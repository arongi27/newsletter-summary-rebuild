from flask import Flask, render_template

app = Flask(__name__)

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
    return render_template("index.html", news_list=news_list)

if __name__ == "__main__":
    app.run(debug=True)
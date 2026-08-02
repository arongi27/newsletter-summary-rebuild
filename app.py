from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "안녕하세요, NewsHub!"

if __name__ == "__main__":
    app.run(debug=True)
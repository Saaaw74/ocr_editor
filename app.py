# app.py
from flask import Flask, render_template
from ocr_routes import ocr_bp
from document_routes import document_bp

app = Flask(__name__, template_folder=".", static_folder="static")

# Регистрация модуля OCR (существующий, диагностический режим "OCR Debug" - не менялся)
app.register_blueprint(ocr_bp)

# Регистрация нового единого pipeline Native/OCR ("Обычный редактор" | "OCR / Скан")
app.register_blueprint(document_bp)

@app.route("/")
def index():
    return render_template("index.html", max_pdf_size_mb=50)

if __name__ == "__main__":
    print("\n Сервер запущен! Перейдите в браузере по адресу: http://127.0.0.1:5000\n")
    #app.run(host="127.0.0.1", port=5000, debug=True)
    app.run(host="0.0.0.0", port=5000, debug=True) #докер 
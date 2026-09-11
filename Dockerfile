FROM python:3.10-slim

# Отключаем буферизацию вывода логов в консоль
ENV PYTHONUNBUFFERED=1

# Устанавливаем системные пакеты:
# - libgl1, libgomp1, libglib2.0-0: нужны для OpenCV/PaddleOCR и графики
# - fonts-liberation, fonts-dejavu-core: нужны для модуля ocr_typography
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    fonts-liberation \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала копируем только requirements.txt для кэширования слоёв pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Прогрев моделей PaddleOCR: скачивает веса прямо в образ при сборке
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(lang='ru', enable_mkldnn=False)"

# Копируем остальной проект
COPY . .

# Порт Flask-сервера
EXPOSE 5000

# Запуск приложения
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "2", "--timeout", "180", "app:app"]
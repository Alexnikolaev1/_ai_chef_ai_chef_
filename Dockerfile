FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Сначала копируем зависимости (кэширование слоёв Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY . .

# Создаём директорию для БД (если нужно)
RUN mkdir -p /data

# Переменная для хранения БД вне контейнера
ENV DB_PATH=/data/ai_chef.db

# Запускаем бота
CMD ["python", "bot.py"]

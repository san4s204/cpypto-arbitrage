# Dockerfile (корень репо)
FROM python:3.11-slim

# 1. рабочая папка
WORKDIR /app

# 2. зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. код проекта
COPY . .

# 4. стартовый модуль
CMD ["python", "-m", "realtime.ws_listener"]

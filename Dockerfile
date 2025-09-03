# Використовуємо офіційний Python образ
FROM python:3.11-slim

# Встановлюємо робочу директорію
WORKDIR /app

# Копіюємо файл з залежностями
COPY requirements.txt .

# Встановлюємо залежності
RUN pip install --no-cache-dir -r requirements.txt

# Копіюємо код додатку
COPY main.py .

# Встановлюємо змінну оточення для Python
ENV PYTHONUNBUFFERED=1

# Відкриваємо порт
EXPOSE 8080

# Запускаємо додаток
CMD ["python", "main.py"]

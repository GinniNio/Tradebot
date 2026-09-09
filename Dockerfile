FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY tradebot ./tradebot
COPY docs ./docs

CMD ["uvicorn", "tradebot.app:app", "--host", "0.0.0.0", "--port", "10000"]


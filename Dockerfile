FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md /app/
RUN pip install --no-cache-dir -e .

COPY app /app/app
COPY scripts /app/scripts
COPY sql /app/sql

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

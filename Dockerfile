FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY app.py requirements.txt ./
COPY templates ./templates

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir .

EXPOSE 8090
EXPOSE 8080

CMD ["uvicorn", "auto_assign.main:app", "--host", "0.0.0.0", "--port", "8090"]

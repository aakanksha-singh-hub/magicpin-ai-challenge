FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vera/ ./vera/

ENV PORT=8080
EXPOSE 8080

# single worker on purpose: context and conversation state are in-process, and
# the harness requires state to persist across calls for the whole test window
CMD ["sh", "-c", "uvicorn vera.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 65"]

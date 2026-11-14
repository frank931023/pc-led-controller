FROM python:3.11-slim

WORKDIR /app

# Log lines show up in docker compose logs right away
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/health', timeout=4)"

# Exactly 1 worker: the status cache and the one-command-per-device lock live in process
# memory, so several workers would each lock on their own. Concurrency comes from threads
# (a long calibrate is fine: gthread's --timeout watches the worker, not each request).
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:5001", "app:app"]

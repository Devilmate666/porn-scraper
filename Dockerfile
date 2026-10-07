FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py scraper.py ./
# Render provides $PORT; gthread lets many streams/downloads run at once
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 --worker-class gthread --threads 32 --timeout 0 --keep-alive 30 app:app"]

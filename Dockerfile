FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py scraper.py ./
# gthread: many concurrent streams/downloads without many processes
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--worker-class", "gthread", "--threads", "32", "--timeout", "0", "--keep-alive", "30", "app:app"]

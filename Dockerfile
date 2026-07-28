FROM python:3.11.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["gunicorn", "bot:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "2"]

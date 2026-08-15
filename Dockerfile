FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Default environment variables
ENV APP_USERNAME=damal
ENV APP_PASSWORD=secretpassword
ENV PORT=29738

EXPOSE $PORT

CMD uvicorn main:app --host 0.0.0.0 --port $PORT
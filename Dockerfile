FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Environment Variables
ENV APP_USERNAME=admin
ENV APP_PASSWORD=adminpassword
ENV PORT=29738
ENV DOWNLOAD_DIR=/downloads
ENV CONFIG_DIR=/config
ENV SESSION_DAYS=30
ENV MAX_DOWNLOAD_MB=150
ENV YTDLP_COOKIES=""

EXPOSE $PORT

CMD uvicorn main:app --host 0.0.0.0 --port $PORT


# ENV APP_USERNAME=admin
# ENV APP_PASSWORD=adminpassword
# ENV PORT=29738
# ENV DOWNLOAD_DIR=/downloads
# ENV CONFIG_DIR=/config
# ENV SESSION_DAYS=30
# ENV MAX_DOWNLOAD_MB=150
# Render uchun Docker image: ichida local Bot API server + bot + ffmpeg.
#
# Nega kerak: Telegram'ning oddiy Bot API'si botga 20 MB dan katta faylni
# yuklab olishga, 50 MB dan katta faylni esa yuborishga ruxsat bermaydi.
# Local Bot API server bilan bu chegara 2 GB gacha ko'tariladi.

# --- 1-bosqich: telegram-bot-api ni yig'ish ---
FROM debian:bookworm-slim AS botapi

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git cmake g++ make zlib1g-dev libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/tdlib/telegram-bot-api.git /src
RUN cmake -S /src -B /src/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /src/build -j"$(nproc)"

# --- 2-bosqich: ishchi obraz ---
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=botapi /src/build/telegram-bot-api /usr/local/bin/telegram-bot-api

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh

ENV PYTHONUNBUFFERED=1
CMD ["/app/start.sh"]

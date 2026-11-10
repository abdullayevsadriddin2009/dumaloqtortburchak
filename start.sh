#!/usr/bin/env bash
# Bot + local Bot API server ni birga ishga tushirish.
#
# API_ID va API_HASH Render'dagi Environment (Secret) dan olinadi —
# kodda saqlanmaydi. Ular bo'lmasa bot avvalgidek api.telegram.org orqali
# ishlaydi (20 MB chegara bilan).
set -u

BOTAPI_PORT="${BOTAPI_PORT:-8081}"
BOTAPI_DIR="${BOTAPI_DIR:-/tmp/telegram-bot-api}"

if [ -n "${API_ID:-}" ] && [ -n "${API_HASH:-}" ]; then
  mkdir -p "$BOTAPI_DIR"
  echo "--- Local Bot API server ishga tushirilmoqda (port $BOTAPI_PORT) ---"
  telegram-bot-api \
    --local \
    --api-id="$API_ID" \
    --api-hash="$API_HASH" \
    --http-address=127.0.0.1 \
    --http-port="$BOTAPI_PORT" \
    --dir="$BOTAPI_DIR" \
    >/tmp/botapi.log 2>&1 &

  # Server tayyor bo'lishini kutamiz (TCP javob berguncha).
  ready=""
  for _ in $(seq 1 90); do
    if curl -s -o /dev/null "http://127.0.0.1:$BOTAPI_PORT/"; then
      ready="1"
      break
    fi
    sleep 1
  done

  if [ -n "$ready" ]; then
    echo "--- Local Bot API server tayyor: 2 GB gacha fayl ishlaydi ---"
    export TELEGRAM_API_URL="http://127.0.0.1:$BOTAPI_PORT"
  else
    echo "--- OGOHLANTIRISH: Bot API server ko'tarilmadi, api.telegram.org ishlatiladi ---"
    tail -n 20 /tmp/botapi.log 2>/dev/null || true
  fi
else
  echo "--- API_ID/API_HASH yo'q: oddiy api.telegram.org (20 MB chegara) ---"
fi

exec python bot.py

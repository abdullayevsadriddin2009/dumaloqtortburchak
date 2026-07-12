#!/usr/bin/env bash
# exit on error
set -o errexit

# Python kutubxonalarini o'rnatish
pip install -r requirements.txt

# Linux uchun static FFmpeg dasturini yuklab olish
if [ ! -d "ffmpeg_bin" ]; then
  mkdir -p ffmpeg_bin
  echo "--- FFmpeg yuklab olinmoqda... ---"
  curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o ffmpeg.tar.xz
  tar -xf ffmpeg.tar.xz -C ffmpeg_bin --strip-components 1
  rm ffmpeg.tar.xz
  echo "--- FFmpeg muvaffaqiyatli yuklab olindi! ---"
fi

#!/bin/bash
# Обход почты каналов в профилях AdsPower с окружением пуллера новой студии (17.10.2026).
# То же окружение, что у ~/gfp.sh (пилот пересылки): токен и адрес студии из
# ~/.vsupload_env_studio, бронь координатора — как у заливки.
#   ~/mail_sweep.sh                      # все профили с привязками
#   ~/mail_sweep.sh --profiles 30 --dry  # один профиль, без отправки
set -u
set -a
. /home/vova/.vsupload_env
. /home/vova/.vsupload_env_studio
set +a
export VSGEN_SERVER=https://studio.13.140.129.66.sslip.io
export UPLOAD_HOST=minsk UPLOAD_WORKER_SLOT=9 UPLOAD_WORK=/home/vova/upload_jobs_rv
export ADSPOWER_RATE_LIMIT_DIR=/home/vova/.local/state/video-studio/adspower-rate
export UPLOAD_PULLER_STATE_DIR=/home/vova/.local/state/video-studio/upload-puller
export UPLOAD_PROFILE_LEASE=1 UPLOAD_PEER_POLICY=lease
cd /home/vova/VideoStudio/src || exit 9
exec /home/vova/VideoStudio/.venv-upload/bin/python -u /home/vova/mail_sweep.py "$@"

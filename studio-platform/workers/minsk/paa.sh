#!/bin/bash
# Сверка «реестр ↔ аккаунты в профиле» с окружением пуллера новой студии (тот же координатор
# броней и AdsPower, что у ssc.sh/gfp.sh). 18.10.2026.
#   ~/paa.sh 78            один профиль
#   ~/paa.sh --all --loop-hours 6
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
exec /home/vova/VideoStudio/.venv-upload/bin/python -u /home/vova/profile_accounts_audit.py "$@"

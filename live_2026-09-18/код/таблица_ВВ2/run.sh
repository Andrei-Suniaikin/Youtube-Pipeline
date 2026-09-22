#!/bin/bash
# ВВ2-таблица: --light каждые 10 мин, --sync раз в сутки. flock не даёт запускам наложиться.
cd /root/ops/ww2_sheet
set -a; . ./.env; set +a
export WW2_SA=/root/ops/ww2_sheet/google_service_account.json
exec /usr/bin/flock -w 900 /root/ops/ww2_sheet/.lock venv/bin/python ww2_sheet.py "$1" >> ww2_sheet.log 2>&1

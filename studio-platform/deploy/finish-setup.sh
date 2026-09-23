#!/bin/bash
# Достроить студию после первого запуска: выдать ей внутренние ключи.
#
#   /srv/studio/app/deploy/finish-setup.sh            # спросит логин и пароль
#   /srv/studio/app/deploy/finish-setup.sh ИМЯ        # логин задан, спросит пароль
#
# Что это вообще. Внутри студии есть модули, которые пишут текст и делают обложки.
# Они работают ОТ ИМЕНИ хозяина студии, и им нужен ключ доступа, который выдаёт сама
# студия при входе. Поэтому здесь спрашивается пароль — тот же, которым вы заходите
# в студию в браузере, никакой не «служебный» и не «dev».
#
# Делается один раз. Пароль спрашивается вводом: в аргументах командной строки он
# остался бы в истории оболочки и в списке процессов.
set -uo pipefail

ROOT="${STUDIO_ROOT:-/srv/studio}"
PORT="${VS_PORT:-8090}"
LOGIN="${1:-}"

if [ -z "$LOGIN" ]; then
    read -r -p "логин в студии: " LOGIN
fi
[ -n "$LOGIN" ] || { echo "логин пустой" >&2; exit 2; }
read -r -s -p "пароль этого логина: " PW; echo
[ -n "$PW" ] || { echo "пароль пустой" >&2; exit 2; }

cd "$ROOT/app" || { echo "нет каталога $ROOT/app" >&2; exit 2; }
VS_DEV_LOGIN="$LOGIN" VS_DEV_PASSWORD="$PW" \
VS_DEV_DB="${VS_SERVER_DB:-$ROOT/data/vsgen.db}" \
VS_PLATFORM_BASE="http://127.0.0.1:$PORT" \
"$ROOT/.venv/bin/python" dev/provision.py
rc=$?
unset PW
if [ $rc -eq 0 ]; then
    echo
    echo "готово: студия достроена, можно работать — https://studio.13.140.129.66.sslip.io/studio/"
else
    echo
    echo "не вышло. Частые причины: неверный пароль, служба не запущена"
    echo "  systemctl status studio-platform"
fi
exit $rc

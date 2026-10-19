#!/bin/bash
# Уборка копий mp4 от подтверждённых заливок на минской машине (19.10.2026).
#
# Зачем на VPS, а не на Минске: решение «эту копию уже можно убрать» принимается по базе
# студии, а база живёт здесь. Минск получает готовый список и только удаляет по нему.
#
# Почему это нужно само, а не руками: пуллер заливки перестаёт брать задания, когда на
# Минске меньше 25 ГБ, и молчит — задания просто стоят в очереди. 19.10 так простояло
# два с половиной часа. Очередь из 50 заданий скачивает около 10 ГБ копий, то есть
# упереться в порог заново — дело одного-двух часов работы конвейера.
#
# Ставится таймером раз в полчаса:
#   cp deploy/minsk_upload_cleanup.{sh,service,timer} /srv/studio/app/deploy/
#   install -m 755 /srv/studio/app/deploy/minsk_upload_cleanup.sh /usr/local/bin/
#   cp /srv/studio/app/deploy/minsk_upload_cleanup.{service,timer} /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now minsk-upload-cleanup.timer
set -u

DB=${VS_DB:-/srv/studio/data/vsgen.db}
MINSK=${VS_MINSK_SSH:-"ssh -p 2222 -o BatchMode=yes vova@127.0.0.1"}
TOOL=${VS_MINSK_TOOL:-/home/vova/upload_jobs_cleanup.py}
# Запас от порога пуллера: не ждём остановки, убираем заранее.
FREE_MIN_GB=${VS_MINSK_FREE_MIN_GB:-40}
DONE_AGE_SEC=${VS_DONE_AGE_SEC:-21600}

say() { echo "[cleanup] $*"; }

free_gb=$($MINSK "df -BG --output=avail / | tail -1 | tr -dc '0-9'" 2>/dev/null)
if [ -z "${free_gb:-}" ]; then
    say "Минск не ответил про место — выхожу, ничего не трогаю"
    exit 1
fi
if [ "$free_gb" -ge "$FREE_MIN_GB" ]; then
    say "свободно ${free_gb} ГБ — запаса хватает, уборка не нужна"
    exit 0
fi
say "свободно ${free_gb} ГБ — меньше запаса ${FREE_MIN_GB}, убираю подтверждённое"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Залито, подтверждено номером ролика и закончено давно: возобновлять нечего.
sqlite3 -readonly "$DB" ".timeout 20000" "
SELECT release_id FROM pub_upload_jobs
WHERE status='done' AND video_id IS NOT NULL AND video_id<>''
  AND finished IS NOT NULL AND finished < strftime('%s','now')-${DONE_AGE_SEC};" \
  > "$tmp/done.txt" || { say "база не читается — выхожу"; exit 1; }

# Полный список заданий: папка, которой здесь нет, осиротела. Неполный список превратил
# бы уборку осиротевших в уборку работающих, поэтому инструмент сам его проверяет.
sqlite3 -readonly "$DB" ".timeout 20000" "SELECT release_id FROM pub_upload_jobs;" \
  > "$tmp/all.txt" || { say "база не читается — выхожу"; exit 1; }

if [ ! -s "$tmp/done.txt" ]; then
    say "подтверждённых заливок пока нет — убирать нечего"
    exit 0
fi

scp -P 2222 -o BatchMode=yes -q "$tmp/done.txt" "$tmp/all.txt" \
    vova@127.0.0.1:/tmp/ || { say "список не передался"; exit 1; }
$MINSK "python3 -I $TOOL /tmp/done.txt --known /tmp/all.txt --apply" 2>&1 | tail -4

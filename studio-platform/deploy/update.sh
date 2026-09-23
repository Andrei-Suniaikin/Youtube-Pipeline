#!/bin/bash
# Обновить студию на боевой машине одной командой — и вернуть прошлую версию, если
# новая не поднялась.
#
#   /srv/studio/app/deploy/update.sh            # обновиться до свежего в текущей ветке
#   /srv/studio/app/deploy/update.sh main       # обновиться до конкретной ветки/метки
#   /srv/studio/app/deploy/update.sh --rollback # вернуться на прошлую версию вручную
#
# Почему с откатом: выкатка без отката однажды оставит студию лежать, а человек у
# терминала будет разбираться с непонятной ошибкой вместо того, чтобы вернуть рабочее.
#
# ВАЖНО про откат и базу. Автооткат возвращает КОД, но не базу. Если новая версия
# успела изменить схему или записать статусы, которых старый код не понимает, откат
# опасен: правило пакета передачи — никогда не откатываться на код, не понимающий уже
# записанные статусы. Поэтому при откате скрипт громко говорит, что надо проверить
# базу глазами, и не делает вид, что всё в порядке.
set -uo pipefail

ROOT="${STUDIO_ROOT:-/srv/studio}"
APP="$ROOT/app"
SERVICE="${STUDIO_SERVICE:-studio-platform}"
HEALTH_URL="${STUDIO_HEALTH_URL:-http://127.0.0.1:${VS_PORT:-8090}/studio/}"
HEALTH_TRIES="${STUDIO_HEALTH_TRIES:-20}"
PREV_FILE="$ROOT/.previous-version"

say() { echo "[обновление] $*"; }
die() { echo "[обновление] ОШИБКА: $*" >&2; exit 1; }

health() {
    local i code
    for ((i = 1; i <= HEALTH_TRIES; i++)); do
        code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$HEALTH_URL" || echo 000)"
        [ "$code" = "200" ] && { say "студия отвечает (попытка $i)"; return 0; }
        sleep 3
    done
    say "студия НЕ отвечает после $HEALTH_TRIES попыток (последний ответ $code)"
    return 1
}

restart_and_check() {
    systemctl restart "$SERVICE" || die "служба не перезапустилась"
    health
}

cd "$APP" 2>/dev/null || die "нет каталога $APP"
[ -d .git ] || die "каталог $APP — не репозиторий. Разверните его через git clone, иначе обновлять нечем"

if [ "${1:-}" = "--rollback" ]; then
    [ -f "$PREV_FILE" ] || die "не помню прошлую версию — откат вручную: git checkout <коммит>"
    PREV="$(cat "$PREV_FILE")"
    say "возвращаю $PREV"
    git checkout -q "$PREV" || die "не смог вернуть $PREV"
    restart_and_check || die "и прошлая версия не отвечает — смотрите journalctl -u $SERVICE"
    say "вернулись на $PREV. Проверьте базу: если новая версия успела записать что-то своё, старый код этого может не понимать"
    exit 0
fi

TARGET="${1:-}"
CURRENT="$(git rev-parse HEAD)"
echo "$CURRENT" > "$PREV_FILE"
say "сейчас $(git rev-parse --short HEAD) ($(git rev-parse --abbrev-ref HEAD))"

git fetch --all --prune || die "не достучался до репозитория"
if [ -n "$TARGET" ]; then
    git checkout -q "$TARGET" || die "нет такой ветки или метки: $TARGET"
    git pull -q --ff-only 2>/dev/null || true
else
    git pull -q --ff-only || die "быстрым перемоткой не обновиться — на машине есть свои правки"
fi
NEW="$(git rev-parse HEAD)"
if [ "$NEW" = "$CURRENT" ]; then
    say "обновлять нечего: уже $(git rev-parse --short HEAD)"
    exit 0
fi
say "ставлю $(git rev-parse --short HEAD)"
say "изменилось файлов: $(git diff --name-only "$CURRENT" "$NEW" | wc -l)"

# Зависимости обновляем, только если список изменился: лишняя установка — это минуты
# простоя на ровном месте.
if ! git diff --quiet "$CURRENT" "$NEW" -- requirements-dev.txt cover/requirements.txt; then
    say "список зависимостей изменился — доставляю"
    "$ROOT/.venv/bin/pip" -q install -r requirements-dev.txt || die "зависимости не встали"
    [ -f cover/requirements.txt ] && "$ROOT/.venv/bin/pip" -q install -r cover/requirements.txt
fi

if restart_and_check; then
    say "готово: $(git rev-parse --short HEAD)"
    exit 0
fi

say "новая версия не поднялась — возвращаю прошлую ($(git rev-parse --short "$CURRENT"))"
journalctl -u "$SERVICE" -n 30 --no-pager | tail -20
git checkout -q "$CURRENT" || die "откат не удался, разбирайтесь руками: git checkout $CURRENT"
if restart_and_check; then
    say "вернулись на прошлую версию. Студия работает, новая версия НЕ выкачена"
    exit 1
fi
die "не поднялась и прошлая версия — смотрите journalctl -u $SERVICE"

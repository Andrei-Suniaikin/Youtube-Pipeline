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
# Каталог кода лежит ПОДКАТАЛОГОМ репозитория (и обычно это ссылка на него), поэтому
# спрашиваем сам git, а не ищем папку .git рядом.
git rev-parse --git-dir >/dev/null 2>&1 \
    || die "каталог $APP не под git. Разверните его через git clone, иначе обновлять нечем"

if [ "${1:-}" = "--rollback" ]; then
    [ -f "$PREV_FILE" ] || die "не помню прошлую версию — откат вручную: git checkout <коммит>"
    PREV="$(cat "$PREV_FILE")"
    say "возвращаю $PREV"
    git checkout -q "$PREV" || die "не смог вернуть $PREV"
    restart_and_check || die "и прошлая версия не отвечает — смотрите journalctl -u $SERVICE"
    say "вернулись на $PREV. Проверьте базу: если новая версия успела записать что-то своё, старый код этого может не понимать"
    exit 0
fi

# ── Идущие заливки: перезапуск их рвёт ───────────────────────────────────────
#
# 04.10.2026: выкатка в 13:04 перезапустила студию, пока минская машина забирала файл
# ролика, — нода получила 502 и сорвала заливку («нода: HTTP Error 502: Bad Gateway»).
# Посреди передачи было бы хуже: ролик ушёл бы в YouTube, а отчёт о нём — нет, и выпуск
# встал бы на ручную сверку. Поэтому ДО перехода на новый код ждём, пока заливки
# закончатся (до STUDIO_UPLOAD_WAIT_SEC), а не дождались — выкатку не делаем: код на
# диске и работающий процесс не должны расходиться. STUDIO_FORCE_RESTART=1 — без ожидания.
UPLOAD_DB="${STUDIO_DB:-$ROOT/data/vsgen.db}"
UPLOAD_WAIT="${STUDIO_UPLOAD_WAIT_SEC:-900}"
UPLOAD_POLL="${STUDIO_UPLOAD_POLL_SEC:-10}"
uploads_running() {
    [ -f "$UPLOAD_DB" ] || { echo 0; return; }
    sqlite3 -readonly -cmd ".timeout 8000" "$UPLOAD_DB" \
        "SELECT COUNT(*) FROM pub_upload_jobs WHERE status='running'" 2>/dev/null || echo "?"
}
wait_uploads() {
    if [ "${STUDIO_FORCE_RESTART:-}" = "1" ]; then
        say "перезапуск без ожидания заливок (STUDIO_FORCE_RESTART=1)"
        return 0
    fi
    local n waited=0
    n="$(uploads_running)"
    while [ "$n" != "0" ]; do
        if [ "$n" = "?" ]; then
            say "не смог спросить базу, идут ли заливки ($UPLOAD_DB) — выкатываю без ожидания"
            return 0
        fi
        if [ "$waited" -ge "$UPLOAD_WAIT" ]; then
            say "заливок в работе: $n, ждал $waited с — выкатку НЕ делаю, код не тронут."
            say "повторите позже или с STUDIO_FORCE_RESTART=1 (сорвёт идущую заливку)"
            return 1
        fi
        [ "$waited" -eq 0 ] && say "идёт заливка ($n) — жду, пока закончится, до $UPLOAD_WAIT с"
        sleep "$UPLOAD_POLL"
        waited=$((waited + UPLOAD_POLL))
        n="$(uploads_running)"
    done
    return 0
}
# Пока ждём — новых заданий заливки не выдавать (студия смотрит на этот файл в
# /api/upload/claim): при потоке заливок «ноль идущих» иначе не наступает. Файл
# убирается при любом выходе скрипта — и после перезапуска, и при отказе.
DRAIN="$(dirname "$UPLOAD_DB")/upload-drain"
if [ -d "$(dirname "$UPLOAD_DB")" ]; then
    touch "$DRAIN" 2>/dev/null && trap 'rm -f "$DRAIN"' EXIT
fi
wait_uploads || exit 2

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

# ── Скрипт запуска: его обязан выкатывать тот же проход ──────────────────────
#
# Юнит запускает НЕ $APP/deploy/start-studio.sh, а копию /srv/studio/start-studio.sh —
# так сделано нарочно: скрипт должен подниматься, даже когда каталог кода на перекладке.
# Но до 02.10.2026 эту копию никто не обновлял, и правка скрипта запуска молча НЕ
# выкатывалась. Найдено так: адрес чата движка уехал на FastGen в репозитории, выкатка
# прошла, служба перезапустилась — а в живом процессе переменной не было. С 23.09 копия
# и репозиторий расходились.
#
# Это ровно та половинчатая выкатка, из-за которой автозвук считался рабочим: серверная
# часть на месте, вторая половина нет. Поэтому копия обновляется ДО перезапуска, с
# резервной копией рядом, и расхождение больше невозможно.
_START_SRC="$APP/deploy/start-studio.sh"
_START_DST="$ROOT/start-studio.sh"
if [ -f "$_START_SRC" ] && ! cmp -s "$_START_SRC" "$_START_DST"; then
    say "скрипт запуска изменился — обновляю копию, которую зовёт systemd"
    cp -p "$_START_DST" "$_START_DST.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    install -m 755 "$_START_SRC" "$_START_DST" || die "копию скрипта запуска не обновить"
fi

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

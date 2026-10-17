#!/bin/bash
# Обновить студию на боевой машине одной командой — и вернуть прошлую версию, если
# новая не поднялась.
#
#   /srv/studio/app/deploy/update.sh            # обновиться до свежего в текущей ветке
#   /srv/studio/app/deploy/update.sh main       # обновиться до конкретной ветки/метки
#   /srv/studio/app/deploy/update.sh --rollback # вернуться на прошлую версию вручную
#   /srv/studio/app/deploy/update.sh --status   # кто сейчас отвечает и у кого движок
#
# Два режима:
#
#   обычный (по умолчанию) — одна служба studio-platform, `systemctl restart`. Студия
#       молчит ~8 секунд; минские пуллеры и сборщик часов в это окно ловят 502.
#   STUDIO_BLUE_GREEN=1 — два экземпляра studio-platform@a / @b (deploy/BLUE_GREEN.md).
#       Новый код поднимается на запасном экземпляре РЯДОМ с работающим, скрипт ждёт его
#       ответа, переключает Caddy и только потом гасит старый. Снаружи простоя нет.
#       Если новый не поднялся — Caddy не трогается, старый работает, код возвращается.
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

# ── Синий/зелёный: где что лежит ─────────────────────────────────────────────
BLUE_GREEN="${STUDIO_BLUE_GREEN-}"                     # 1 — два экземпляра; 0 — настаивать на старой схеме
INST_DIR="${STUDIO_INSTANCES_DIR:-$ROOT/instances}"    # a.env, b.env, active
ACTIVE_FILE="$INST_DIR/active"                         # имя активного экземпляра: a или b
UPSTREAM_FILE="${STUDIO_CADDY_UPSTREAM:-/etc/caddy/studio-upstream.caddy}"
CADDYFILE="${STUDIO_CADDYFILE:-/etc/caddy/Caddyfile}"
CADDY_ADMIN="${STUDIO_CADDY_ADMIN:-http://127.0.0.1:2019}"
PUBLIC_URL="${STUDIO_PUBLIC_URL:-https://studio.13.140.129.66.sslip.io}"
SWITCH_TRIES="${STUDIO_SWITCH_TRIES:-15}"              # по 2 с: сколько ждать, пока Caddy применит новый upstream
OLD_STOP_WAIT="${STUDIO_OLD_STOP_WAIT_SEC:-${STUDIO_UPLOAD_WAIT_SEC:-900}}"

say() { echo "[обновление] $*"; }
die() { echo "[обновление] ОШИБКА: $*" >&2; exit 1; }

health() {
    # health [URL] — ждать 200 от URL (по умолчанию — HEALTH_URL одиночной службы).
    local url="${1:-$HEALTH_URL}" i code
    for ((i = 1; i <= HEALTH_TRIES; i++)); do
        code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url" || echo 000)"
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

# ── Синий/зелёный: вспомогательные ───────────────────────────────────────────
bg_installed() { [ -f "$ACTIVE_FILE" ] && [ -f "$INST_DIR/a.env" ] && [ -f "$INST_DIR/b.env" ]; }
bg_port() { sed -n 's/^VS_PORT=//p' "$INST_DIR/$1.env" | tail -1 | tr -d '[:space:]'; }
bg_other() { case "$1" in a) echo b ;; b) echo a ;; *) echo "" ;; esac; }
bg_unit_active() { systemctl is-active --quiet "studio-platform@$1"; }
# Что отвечает ручка /_instance данного порта (поле по имени). Пусто — не ответила.
bg_field() {   # bg_field URL поле
    curl -s -m 10 "$1" 2>/dev/null | tr -d '\n' \
        | sed -n "s/.*\"$2\": *\"\([^\"]*\)\".*/\1/p" | head -1
}
bg_engine_state() { curl -s -m 10 "http://127.0.0.1:$1/_instance" 2>/dev/null | tr -d '\n ' \
        | sed -n 's/.*"engine":{[^}]*"state":"\([a-z]*\)".*/\1/p' | head -1; }
bg_upstream_port() { sed -n 's/.*127\.0\.0\.1:\([0-9][0-9]*\).*/\1/p' "$UPSTREAM_FILE" | head -1; }
# Какой порт студии стоит в ЖИВОЙ конфигурации Caddy (через его admin API, а не по файлу).
bg_caddy_dials() { curl -s -m 10 "$CADDY_ADMIN/config/" 2>/dev/null | tr -d ' \n' \
        | grep -o '"dial":"127\.0\.0\.1:[0-9]*"' | sed 's/.*://; s/"//' | sort -u | tr '\n' ' '; }

bg_require() {
    # Всё, без чего выкатка без простоя не имеет смысла. Ничего не чинит — говорит, чего нет.
    bg_installed || die "синий/зелёный не установлен: нет $ACTIVE_FILE (и/или a.env, b.env). Установка — deploy/BLUE_GREEN.md"
    systemctl cat studio-platform@a >/dev/null 2>&1 || die "нет шаблонного юнита studio-platform@.service (deploy/BLUE_GREEN.md, шаг 2)"
    [ -f "$UPSTREAM_FILE" ] || die "нет $UPSTREAM_FILE — Caddy ещё не переведён на сниппет (deploy/BLUE_GREEN.md, шаг 4)"
    command -v caddy >/dev/null || die "нет команды caddy — нечем проверить конфигурацию перед reload"
    ACTIVE="$(tr -d '[:space:]' < "$ACTIVE_FILE")"
    IDLE="$(bg_other "$ACTIVE")"
    [ -n "$IDLE" ] || die "в $ACTIVE_FILE должно быть a или b, а там «$ACTIVE»"
    ACTIVE_PORT="$(bg_port "$ACTIVE")"; IDLE_PORT="$(bg_port "$IDLE")"
    [ -n "$ACTIVE_PORT" ] && [ -n "$IDLE_PORT" ] || die "в $INST_DIR/{a,b}.env нет VS_PORT"
    # Три источника должны сходиться: файл active, работающий юнит и upstream Caddy.
    # Расходятся — значит, кто-то что-то делал руками; гадать, кто прав, скрипт не будет.
    bg_unit_active "$ACTIVE" || die "по $ACTIVE_FILE активен $ACTIVE, но studio-platform@$ACTIVE не работает. Разберитесь руками: update.sh --status"
    local up; up="$(bg_upstream_port)"
    [ "$up" = "$ACTIVE_PORT" ] || die "по $ACTIVE_FILE активен $ACTIVE (порт $ACTIVE_PORT), а в $UPSTREAM_FILE порт $up. Разберитесь руками: update.sh --status"
}

bg_status() {
    echo "режим: синий/зелёный $(bg_installed && echo 'установлен' || echo 'НЕ установлен')"
    echo "код:   $(git -C "$APP" rev-parse --short HEAD 2>/dev/null) ($(git -C "$APP" rev-parse --abbrev-ref HEAD 2>/dev/null))"
    if bg_installed; then
        echo "active-файл: $(tr -d '[:space:]' < "$ACTIVE_FILE")"
        echo "Caddy, файл:  порт $(bg_upstream_port) ($UPSTREAM_FILE)"
        echo "Caddy, живой: порты $(bg_caddy_dials)"
        local n p st eng ver
        for n in a b; do
            p="$(bg_port "$n")"
            st="$(systemctl is-active "studio-platform@$n" 2>/dev/null)"
            eng="$(bg_engine_state "$p")"; ver="$(bg_field "http://127.0.0.1:$p/_instance" version)"
            echo "  экземпляр $n: порт $p, служба $st, движок ${eng:-—}, версия ${ver:-—}, автозапуск $(systemctl is-enabled "studio-platform@$n" 2>/dev/null)"
        done
        echo "  через Caddy отвечает: $(bg_field "$PUBLIC_URL/_instance" instance || echo '—')"
    fi
    echo "одиночная служба $SERVICE: $(systemctl is-active "$SERVICE" 2>/dev/null) / автозапуск $(systemctl is-enabled "$SERVICE" 2>/dev/null)"
    local lock="$ROOT/data/engine.lock"
    [ -f "$lock" ] && echo "замок движка: $(head -c 200 "$lock")"
}

bg_write_upstream() {
    # Переписать порт в сниппете Caddy атомарно (tmp + mv): Caddy читает файл при reload,
    # и полузаписанный файл означал бы reload с ошибкой.
    local port="$1" tmp
    tmp="$(mktemp "$UPSTREAM_FILE.XXXXXX")" || return 1
    sed "s/127\.0\.0\.1:[0-9][0-9]*/127.0.0.1:$port/" "$UPSTREAM_FILE" > "$tmp" || { rm -f "$tmp"; return 1; }
    grep -q "127\.0\.0\.1:$port" "$tmp" || { rm -f "$tmp"; return 1; }
    chmod --reference="$UPSTREAM_FILE" "$tmp" 2>/dev/null
    mv -f "$tmp" "$UPSTREAM_FILE"
}

bg_caddy_switch_to() {
    # Переключить Caddy на порт $1 и убедиться по его живой конфигурации, что он
    # переключился. Возврат 1 — не переключился, файл возвращён как был.
    local port="$1" other="$2" i dials
    cp -p "$UPSTREAM_FILE" "$UPSTREAM_FILE.prev" || return 1
    bg_write_upstream "$port" || { cp -p "$UPSTREAM_FILE.prev" "$UPSTREAM_FILE"; say "не смог переписать $UPSTREAM_FILE"; return 1; }
    if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
        say "Caddy не принимает конфигурацию с портом $port — возвращаю прежний файл, Caddy не трогаю"
        caddy validate --config "$CADDYFILE" --adapter caddyfile 2>&1 | tail -5
        cp -p "$UPSTREAM_FILE.prev" "$UPSTREAM_FILE"
        return 1
    fi
    # reload без ожидания: Caddy применяет новую конфигурацию сразу, а команда reload
    # может висеть, пока старые соединения дорабатывают длинные ответы (файл ролика
    # ноде). На переключение это не влияет — проверяем его по живой конфигурации.
    systemctl reload --no-block caddy
    for ((i = 1; i <= SWITCH_TRIES; i++)); do
        dials="$(bg_caddy_dials)"
        case " $dials " in
            *" $port "*)
                case " $dials " in
                    *" $other "*) ;;                          # старый порт ещё в живой конфигурации — ждём
                    *) say "Caddy переключён на порт $port (проверено по живой конфигурации)"; return 0 ;;
                esac ;;
        esac
        sleep 2
    done
    say "Caddy так и не переключился на порт $port (живые upstream: ${dials:-не удалось прочитать}) — возвращаю прежний файл"
    cp -p "$UPSTREAM_FILE.prev" "$UPSTREAM_FILE"
    systemctl reload --no-block caddy
    return 1
}

bg_retire_old() {
    # Старый экземпляр: вежливая остановка. uvicorn перестаёт принимать новые
    # соединения (их уже и нет — Caddy смотрит на новый) и дорабатывает начатые ответы.
    # Ждём до OLD_STOP_WAIT, дальше не держим человека у терминала: systemd добьёт
    # процесс сам по TimeoutStopSec юнита, а замок движка перейдёт к новому автоматически.
    local waited=0 st
    if [ "${STUDIO_FORCE_RESTART:-}" = "1" ]; then
        say "останавливаю старый экземпляр $ACTIVE без ожидания (STUDIO_FORCE_RESTART=1)"
        systemctl stop --no-block "studio-platform@$ACTIVE"
        sleep 15
        if bg_unit_active "$ACTIVE"; then
            say "старый ещё жив через 15 с — убиваю (сорвёт идущую заливку, вы просили силой)"
            systemctl kill -s SIGKILL "studio-platform@$ACTIVE"
        fi
    else
        systemctl stop --no-block "studio-platform@$ACTIVE"
        while bg_unit_active "$ACTIVE"; do
            if [ "$waited" -ge "$OLD_STOP_WAIT" ]; then
                say "старый экземпляр $ACTIVE всё ещё дорабатывает начатые ответы (ждал $waited с)."
                say "Это не ошибка: новый уже обслуживает всех. systemd остановит старый сам, движок"
                say "перейдёт к новому автоматически. Проверить позже: update.sh --status"
                return 0
            fi
            [ "$waited" -eq 0 ] && say "старый экземпляр $ACTIVE дорабатывает начатые ответы (заливка?) — жду до $OLD_STOP_WAIT с"
            sleep 5; waited=$((waited + 5))
        done
    fi
    say "старый экземпляр $ACTIVE остановлен"
    # Движок: замок освободился, новый экземпляр берёт его в пределах пары секунд.
    for ((waited = 0; waited < 30; waited += 2)); do
        st="$(bg_engine_state "$IDLE_PORT")"
        [ "$st" = "owner" ] && { say "движок (выпуски, планировщик, очереди) перешёл к $IDLE"; return 0; }
        sleep 2
    done
    say "ВНИМАНИЕ: через 30 с после остановки старого движок у $IDLE всё ещё «${st:-?}» — смотрите journalctl -u studio-platform@$IDLE"
    return 0
}

bg_undo_new() {
    # Новый экземпляр не годится: гасим его, возвращаем код; старый всё это время работал.
    journalctl -u "studio-platform@$IDLE" -n 30 --no-pager | tail -20
    systemctl stop "studio-platform@$IDLE"
    git checkout -q "$1" || die "откат кода не удался, разбирайтесь руками: git checkout $1"
    if [ -n "${_START_BAK:-}" ] && [ -f "$_START_BAK" ]; then
        install -m 755 "$_START_BAK" "$_START_DST" && say "копия скрипта запуска возвращена из $_START_BAK"
    fi
    say "старый экземпляр $ACTIVE не трогали — студия работает прежней версией $(git rev-parse --short HEAD)"
}

bg_deploy_checked_out() {
    # Код уже нужный на диске. Поднять запасной, дождаться, переключить, погасить старый.
    local me; me="$(git rev-parse --short HEAD)"
    if bg_unit_active "$IDLE"; then
        say "запасной экземпляр $IDLE почему-то работает (остаток прошлой выкатки) — останавливаю"
        systemctl stop "studio-platform@$IDLE" || die "не смог остановить studio-platform@$IDLE"
    fi
    say "поднимаю $me на запасном экземпляре $IDLE (порт $IDLE_PORT); активный $ACTIVE (порт $ACTIVE_PORT) работает"
    systemctl start "studio-platform@$IDLE" || { say "studio-platform@$IDLE не стартовал"; return 1; }
    health "http://127.0.0.1:$IDLE_PORT/healthz" || return 1
    local ver; ver="$(bg_field "http://127.0.0.1:$IDLE_PORT/_instance" version)"
    if [ -n "$ver" ] && [ "$ver" != "$me" ]; then
        say "ВНИМАНИЕ: новый экземпляр отвечает версией «$ver», а на диске $me"
    fi
    local eng; eng="$(bg_engine_state "$IDLE_PORT")"
    [ "$eng" = "standby" ] || say "ВНИМАНИЕ: движок у нового — «${eng:-?}», ожидалось standby (замок должен держать старый)"
    bg_caddy_switch_to "$IDLE_PORT" "$ACTIVE_PORT" || return 1
    echo "$IDLE" > "$ACTIVE_FILE"
    systemctl enable -q "studio-platform@$IDLE" 2>/dev/null
    systemctl disable -q "studio-platform@$ACTIVE" 2>/dev/null
    say "активный экземпляр теперь $IDLE"
    bg_retire_old
    return 0
}

cd "$APP" 2>/dev/null || die "нет каталога $APP"
# Каталог кода лежит ПОДКАТАЛОГОМ репозитория (и обычно это ссылка на него), поэтому
# спрашиваем сам git, а не ищем папку .git рядом.
git rev-parse --git-dir >/dev/null 2>&1 \
    || die "каталог $APP не под git. Разверните его через git clone, иначе обновлять нечем"

if [ "${1:-}" = "--status" ]; then
    bg_status
    exit 0
fi

# ── Предохранитель: после перехода на синий/зелёный старый режим опасен ─────
# `systemctl restart studio-platform` поднял бы ТРЕТИЙ процесс на 8090, на который Caddy
# не смотрит, и доложил бы «готово». Поэтому, если переход сделан, без явного слова
# старый режим не запускается. STUDIO_BLUE_GREEN=0 — «я знаю, что делаю».
if [ -z "$BLUE_GREEN" ] && bg_installed; then
    die "студия переведена на синий/зелёный ($ACTIVE_FILE есть). Запускайте: STUDIO_BLUE_GREEN=1 $0 ${1:-}
Если нарочно нужна старая схема с одной службой — STUDIO_BLUE_GREEN=0 $0 ${1:-}"
fi
if [ "$BLUE_GREEN" = "1" ]; then
    bg_require
fi

# Флаг «идёт выкатка» для рерайта: прерванным задачам не засчитывается попытка
# продолжения (rewrite/migrations/api_v1_001.py, planned_restart). Раньше флагом служил
# файл upload-drain, теперь — свой файл (start-studio.sh задаёт VS_PLANNED_RESTART_FILE);
# ставим оба, чтобы старый и новый код поняли одинаково. Свежесть — полчаса, забытый
# файл сам перестаёт действовать.
PLANNED="$(dirname "${STUDIO_DB:-$ROOT/data/vsgen.db}")/planned-restart"
touch "$PLANNED" 2>/dev/null || true

if [ "${1:-}" = "--rollback" ]; then
    [ -f "$PREV_FILE" ] || die "не помню прошлую версию — откат вручную: git checkout <коммит>"
    PREV="$(cat "$PREV_FILE")"
    say "возвращаю $PREV"
    if [ "$BLUE_GREEN" = "1" ]; then
        NOW="$(git rev-parse HEAD)"
        git checkout -q "$PREV" || die "не смог вернуть $PREV"
        if bg_deploy_checked_out; then
            say "вернулись на $PREV без простоя. Проверьте базу: если новая версия успела записать что-то своё, старый код этого может не понимать"
            exit 0
        fi
        bg_undo_new "$NOW"
        die "прошлая версия не поднялась на запасном экземпляре — смотрите journalctl -u studio-platform@$IDLE"
    fi
    trap 'rm -f "$PLANNED"' EXIT
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
#
# В режиме синий/зелёный этого ожидания нет: никто никого не рвёт. Новые запросы уходят
# на новый экземпляр, а старый дорабатывает начатые ответы (в том числе отдачу файла
# ноде) и останавливается вежливо — см. bg_retire_old и TimeoutStopSec юнита.
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
if [ "$BLUE_GREEN" != "1" ]; then
    # Пока ждём — новых заданий заливки не выдавать (студия смотрит на этот файл в
    # /api/upload/claim): при потоке заливок «ноль идущих» иначе не наступает. Файл
    # убирается при любом выходе скрипта — и после перезапуска, и при отказе.
    DRAIN="$(dirname "$UPLOAD_DB")/upload-drain"
    if [ -d "$(dirname "$UPLOAD_DB")" ]; then
        touch "$DRAIN" 2>/dev/null && trap 'rm -f "$DRAIN" "$PLANNED"' EXIT
    fi
    wait_uploads || exit 2
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
_START_BAK=""
if [ -f "$_START_SRC" ] && ! cmp -s "$_START_SRC" "$_START_DST"; then
    say "скрипт запуска изменился — обновляю копию, которую зовёт systemd"
    _START_BAK="$_START_DST.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$_START_DST" "$_START_BAK" 2>/dev/null || _START_BAK=""
    install -m 755 "$_START_SRC" "$_START_DST" || die "копию скрипта запуска не обновить"
fi

# Зависимости обновляем, только если список изменился: лишняя установка — это минуты
# простоя на ровном месте.
if ! git diff --quiet "$CURRENT" "$NEW" -- requirements-dev.txt cover/requirements.txt; then
    say "список зависимостей изменился — доставляю"
    "$ROOT/.venv/bin/pip" -q install -r requirements-dev.txt || die "зависимости не встали"
    [ -f cover/requirements.txt ] && "$ROOT/.venv/bin/pip" -q install -r cover/requirements.txt
fi

# ── Синий/зелёный ────────────────────────────────────────────────────────────
if [ "$BLUE_GREEN" = "1" ]; then
    if bg_deploy_checked_out; then
        say "готово без простоя: $(git rev-parse --short HEAD) на экземпляре $IDLE"
        exit 0
    fi
    say "новая версия не поднялась на запасном экземпляре — Caddy не трогал, старый экземпляр $ACTIVE работает"
    bg_undo_new "$CURRENT"
    exit 1
fi

# ── Обычный режим: одна служба, перезапуск ───────────────────────────────────
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

#!/bin/bash
# Поднять платформу локально: ОДИН процесс, ОДИН порт.
#
# Было три сервиса на трёх портах. Теперь они смонтированы в одно приложение
# (web/platform.py):
#
#   http://127.0.0.1:8080/          кабинет  — весь интерфейс и API, пути прежние
#   http://127.0.0.1:8080/studio/   пульт    — новый единый интерфейс
#   http://127.0.0.1:8080/rewrite/  Rewrite Studio
#   http://127.0.0.1:8080/cover/    Cover Studio
#
# Ctrl+C останавливает всё разом — потому что «всё» теперь один процесс.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${VS_PORT:-8080}"
[ -f "$ROOT/dev/vsgen.dev.db" ] || "$PY" "$ROOT/dev/make_db.py"
# Кабинет читает воркеры по зашитому пути ~/VideoStudio/src, поэтому ему подсовывается
# изолированный HOME внутри проекта: dev/home/VideoStudio/src → symlink на workers/.
mkdir -p "$ROOT/dev/home/VideoStudio" "$ROOT/dev/data/tenants"
[ -e "$ROOT/dev/home/VideoStudio/src" ] || ln -s ../../../workers "$ROOT/dev/home/VideoStudio/src"

# Соседи теперь в том же процессе под префиксами — кабинет и пульт должны звать их туда,
# а не на 8899/8900. Прямой запуск отдельными сервисами при этом остаётся возможным:
# адреса берутся из окружения, а не зашиты.
export VS_REWRITE_BASE="http://127.0.0.1:$PORT/rewrite"
export VS_COVER_BASE="http://127.0.0.1:$PORT/cover"
export VS_CABINET_BASE="http://127.0.0.1:$PORT"
export HOME="$ROOT/dev/home"
export VS_SERVER_DB="$ROOT/dev/vsgen.dev.db"
export VS_DEV_DB="$ROOT/dev/vsgen.dev.db"
export VS_TENANTS_DIR="$ROOT/dev/data/tenants"
# Планировщик по умолчанию ВЫКЛЮЧЕН: здесь разбирают интерфейс, а не тратят деньги.
# Задачи стоят в очереди и никто их не берёт. Боевой прогон — dev/run-live.sh,
# он выставляет эту переменную пустой и планировщик поднимается.
export VS_SERVER_DISABLE_BACKGROUND="${VS_SERVER_DISABLE_BACKGROUND-1}"
# Пульт по умолчанию ставит куку Secure — правильно за Caddy, но по http://127.0.0.1
# её сохранит не всякий браузер. Снимать защиту должен тот, кто знает, что он
# в песочнице, поэтому послабление живёт здесь, а не в коде.
export VS_DEV_INSECURE_COOKIE=1
# После слияния личности админ у соседей опознаётся по общему id (tenant_id кабинета),
# а не по зашитому литералу. Значения должны совпадать, иначе админ теряет права молча.
export REWRITE_ADMIN_USER_ID="${VS_DEV_TENANT:-dev}"
export COVER_ADMIN_USER_ID="${VS_DEV_TENANT:-dev}"

trap 'kill 0' EXIT
( cd "$ROOT" && "$PY" -m uvicorn web.platform:app --port "$PORT" --log-level warning ) &

# Тревоги — отдельным процессом, а не потоком внутри платформы: в бою это systemd-таймер,
# и падение опроса не должно уносить с собой интерфейс. Запускается только когда телеграм
# настроен, иначе крутить цикл в пустоту незачем.
if grep -qE '^VS_TG_TOKEN=.+' "$ROOT/.env" 2>/dev/null && grep -qE '^VS_TG_CHAT=.+' "$ROOT/.env" 2>/dev/null; then
  ( cd "$ROOT" && "$PY" dev/alerts.py --loop --every 60 ) &
  ALERTS="  тревоги  каждые 60 с → телеграм"
else
  ALERTS="  тревоги  выключены: нет VS_TG_TOKEN / VS_TG_CHAT в .env (см. .env.example)"
fi

echo
echo "  пульт    http://127.0.0.1:$PORT/studio/    (новый единый интерфейс)"
echo "  кабинет  http://127.0.0.1:$PORT/           (боевой VSGen, пути прежние)"
echo "  rewrite  http://127.0.0.1:$PORT/rewrite/"
echo "  cover    http://127.0.0.1:$PORT/cover/"
echo "  что смонтировано: http://127.0.0.1:$PORT/_platform"
echo "$ALERTS"
echo
echo "  вход — логин и пароль кабинета (dev / VS_DEV_PASSWORD)."
echo "  ключи соседей и пароль живут в базе; после dev/make_db.py их нужно выдать заново:"
echo "    .venv/bin/python dev/provision.py"
echo
wait

#!/bin/bash
# Поднять все три сервиса локально. Ctrl+C останавливает все.
#
# Каждый сервис запускается ИЗ СВОЕГО каталога — так же, как в production
# (systemd: WorkingDirectory=/srv/studio/<сервис>). Это обязательно: rewrite/auth.py
# резолвит sessions.db относительно рабочего каталога, а server.py — относительно
# каталога модуля. Запуск из другого места разводит их в две разные базы.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -f "$ROOT/dev/vsgen.dev.db" ] || "$PY" "$ROOT/dev/make_db.py"
# Кабинет читает воркеры по зашитому пути ~/VideoStudio/src, поэтому ему подсовывается
# изолированный HOME внутри проекта: dev/home/VideoStudio/src → symlink на workers/.
mkdir -p "$ROOT/dev/home/VideoStudio" "$ROOT/dev/data/tenants"
[ -e "$ROOT/dev/home/VideoStudio/src" ] || ln -s ../../../workers "$ROOT/dev/home/VideoStudio/src"
trap 'kill 0' EXIT
( cd "$ROOT/server" && HOME="$ROOT/dev/home" VS_SERVER_DB="$ROOT/dev/vsgen.dev.db" VS_TENANTS_DIR="$ROOT/dev/data/tenants" \
      VS_SERVER_DISABLE_BACKGROUND=1 \
      "$PY" -m uvicorn main:app --port 8800 --log-level warning ) &
( cd "$ROOT/cover"   && "$PY" -m uvicorn cover_server:app --port 8900 --log-level warning ) &
( cd "$ROOT/rewrite" && "$PY" -m uvicorn server:app       --port 8899 --log-level warning ) &
# VS_DEV_INSECURE_COOKIE: пульт по умолчанию ставит куку Secure — правильно за Caddy,
# но по http://127.0.0.1 её сохранит не всякий браузер. Снимать защиту должен тот,
# кто знает, что он в песочнице, поэтому послабление живёт здесь, а не в коде.
( cd "$ROOT"         && VS_DEV_INSECURE_COOKIE=1 \
      "$PY" -m uvicorn web.app:app      --port 8010 --log-level warning ) &
# Тревоги — отдельный процесс, а не поток внутри пульта: в бою это systemd-таймер,
# и падение опроса не должно уносить с собой интерфейс. Запускается только когда
# телеграм настроен, иначе крутить цикл в пустоту незачем.
if grep -qE '^VS_TG_TOKEN=.+' "$ROOT/.env" 2>/dev/null && grep -qE '^VS_TG_CHAT=.+' "$ROOT/.env" 2>/dev/null; then
  ( cd "$ROOT" && "$PY" dev/alerts.py --loop --every 60 ) &
  ALERTS="  тревоги  каждые 60 с → телеграм"
else
  ALERTS="  тревоги  выключены: нет VS_TG_TOKEN / VS_TG_CHAT в .env (см. .env.example)"
fi
echo
echo "  пульт    http://127.0.0.1:8010   (новый)"
echo "$ALERTS"
echo "  кабинет  http://127.0.0.1:8800   (боевой VSGen)"
echo "  rewrite  http://127.0.0.1:8899"
echo "  cover    http://127.0.0.1:8900"
echo
echo "  вход в пульт — логин и пароль кабинета (dev / VS_DEV_PASSWORD)."
echo "  ключи соседей и пароль живут в базе; после dev/make_db.py их нужно выдать заново:"
echo "    .venv/bin/python dev/provision.py"
echo
wait

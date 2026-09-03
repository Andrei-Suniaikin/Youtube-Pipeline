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
[ -f "$ROOT/dev/cabinet.db" ] || cp "$ROOT/dev/vsgen.dev.db" "$ROOT/dev/cabinet.db"
mkdir -p "$ROOT/dev/home/VideoStudio" "$ROOT/dev/data/tenants"
[ -e "$ROOT/dev/home/VideoStudio/src" ] || ln -s ../../../workers "$ROOT/dev/home/VideoStudio/src"
trap 'kill 0' EXIT
( cd "$ROOT/server" && HOME="$ROOT/dev/home" VS_SERVER_DB="$ROOT/dev/cabinet.db" VS_TENANTS_DIR="$ROOT/dev/data/tenants" \
      VS_SERVER_DISABLE_BACKGROUND=1 \
      "$PY" -m uvicorn main:app --port 8800 --log-level warning ) &
( cd "$ROOT/cover"   && "$PY" -m uvicorn cover_server:app --port 8900 --log-level warning ) &
( cd "$ROOT/rewrite" && "$PY" -m uvicorn server:app       --port 8899 --log-level warning ) &
( cd "$ROOT"         && "$PY" -m uvicorn web.app:app      --port 8010 --log-level warning ) &
echo
echo "  пульт    http://127.0.0.1:8010   (новый)"
echo "  кабинет  http://127.0.0.1:8800   (боевой VSGen)"
echo "  rewrite  http://127.0.0.1:8899"
echo "  cover    http://127.0.0.1:8900"
echo
wait

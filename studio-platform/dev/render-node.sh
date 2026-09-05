#!/bin/bash
# Рендер-нода на ЭТОЙ машине.
#
# Зачем отдельно: платформа рендер не делает. Её планировщик берёт из очереди всё,
# кроме `render` (main.py:10768) — рендер забирает отдельная машина, опрашивая
# /api/render/claim. В бою это минский бокс. Без такой ноды очередь доходит до
# «Сборка» и встаёт: mp4 делать некому.
#
#   dev/run.sh          в одном окне   (платформа)
#   dev/render-node.sh  в другом       (рендер)
#
# Что здесь запускается — БОЕВОЙ рендерер, а не его копия: render_worker из живого
# среза 02.09 побайтово равен тому, что крутится в Минске. Проверять надо то, что
# работает в бою, поэтому файл берётся оттуда и не копируется в репозиторий:
# четвёртая копия шеститысячестрочного движка разъедется с остальными тремя.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LIVE="$(cd "$ROOT/.." && pwd)/live_2026-09-02"
PORT="${VS_PORT:-8080}"

WORKER="${RENDER_WORKER:-$LIVE/рендер/render_worker.МИНСК-боевой.py}"
PULLER="${RENDER_PULLER:-$LIVE/пуллеры/render_puller.py}"
for f in "$WORKER" "$PULLER"; do
  [ -f "$f" ] || { echo "  НЕТ ФАЙЛА: $f"; exit 1; }
done
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "  НЕТ ffmpeg — рендерить нечем. Установить: brew install ffmpeg"
  exit 1
fi

# Токен общий с платформой. Пустой токен сервер отвергает всегда (main.py:12856),
# поэтому значение по умолчанию здесь такое же, как в dev/run.sh.
if [ -z "$PULLER_TOKEN" ] && [ -f "$ROOT/.env" ]; then
  PULLER_TOKEN="$(grep -E '^PULLER_TOKEN=.+' "$ROOT/.env" | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
fi
export PULLER_TOKEN="${PULLER_TOKEN:-dev-render-node}"
export VSGEN_SERVER="${VSGEN_SERVER:-http://127.0.0.1:$PORT}"
export RENDER_WORKER="$WORKER"
export PULLER_WORK="${PULLER_WORK:-$ROOT/dev/render_jobs}"
export PULLER_POLL="${PULLER_POLL:-3}"
mkdir -p "$PULLER_WORK"

echo
echo "  РЕНДЕР-НОДА"
echo "  платформа  $VSGEN_SERVER"
echo "  рендерер   $(basename "$WORKER")"
echo "  работа в   $PULLER_WORK"
echo "  ffmpeg     $(command -v ffmpeg)"
echo
echo "  Готовый ролик уедет обратно на платформу сам: Проекты → карточка → «Сборка»."
echo
exec "$PY" "$PULLER"

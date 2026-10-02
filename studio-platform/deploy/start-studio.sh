#!/bin/bash
# Запуск единой студии на боевой машине — рядом с тремя старыми сервисами, не вместо них.
#
# Почему отдельный скрипт, а не просто ExecStart=uvicorn: часть окружения выводится из
# файла ключей (ключ FastGen для текста, ключ стороннего прокси для обложек и промптов),
# ровно как в `dev/run.sh`. Повторять эту логику внутри юнита systemd негде.
#
# Всё, что можно задать снаружи, задаётся снаружи: юнит подсовывает
# /srv/studio/studio.env, и любое значение оттуда выигрывает у здешних умолчаний.
set -euo pipefail

ROOT="${STUDIO_ROOT:-/srv/studio}"
APP="$ROOT/app"
PY="$ROOT/.venv/bin/python"
PORT="${VS_PORT:-8090}"

# --- свои данные, отдельно от боевых сервисов -------------------------------------
# Общий /data/tenants и базы в /root принадлежат старой системе. Пересечение здесь
# означало бы, что уборка одной системы сносит проекты другой.
export VS_SERVER_DB="${VS_SERVER_DB:-$ROOT/data/vsgen.db}"
export VS_DEV_DB="${VS_DEV_DB:-$VS_SERVER_DB}"
export VS_TENANTS_DIR="${VS_TENANTS_DIR:-$ROOT/data/tenants}"
export VS_SERVER_KEYS_PATH="${VS_SERVER_KEYS_PATH:-$ROOT/keys/video_studio_api_keys.json}"
export SESSION_DB="${SESSION_DB:-$ROOT/data/rewrite-sessions.db}"
export REWRITE_DB_PATH="${REWRITE_DB_PATH:-$ROOT/data/rewrite-jobs.db}"
export REWRITE_OUTPUT_DIR="${REWRITE_OUTPUT_DIR:-$ROOT/data/rewrite-outputs}"
export REWRITE_UPLOAD_DIR="${REWRITE_UPLOAD_DIR:-$ROOT/data/rewrite-uploads}"
export REWRITE_PRESETS_DIR="${REWRITE_PRESETS_DIR:-$ROOT/data/rewrite-presets}"
export REWRITE_JOB_LOG_DIR="${REWRITE_JOB_LOG_DIR:-$ROOT/data/rewrite-job-logs}"
export REWRITE_SUBSCRIPTIONS_DIR="${REWRITE_SUBSCRIPTIONS_DIR:-$ROOT/data/text-subscriptions}"
export COVER_DB_PATH="${COVER_DB_PATH:-$ROOT/data/cover.db}"
export COVER_OUTPUT_DIR="${COVER_OUTPUT_DIR:-$ROOT/data/cover-outputs}"
export COVER_UPLOAD_DIR="${COVER_UPLOAD_DIR:-$ROOT/data/cover-uploads}"
export COVER_PRESETS_DIR="${COVER_PRESETS_DIR:-$ROOT/data/cover-presets}"
export VIDEO_STUDIO_SHARED_DIR="${VIDEO_STUDIO_SHARED_DIR:-$APP/shared}"
export VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR="${VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR:-$ROOT/data/credential-budget}"

mkdir -p "$ROOT/data" "$VS_TENANTS_DIR" "$ROOT/keys" "$ROOT/home/VideoStudio" \
         "$REWRITE_OUTPUT_DIR" "$REWRITE_UPLOAD_DIR" "$REWRITE_PRESETS_DIR" \
         "$REWRITE_JOB_LOG_DIR" "$COVER_OUTPUT_DIR" "$COVER_UPLOAD_DIR" "$COVER_PRESETS_DIR"
mkdir -p "$VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR" && chmod 700 "$VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"
[ -f "$VS_SERVER_KEYS_PATH" ] || { echo '{}' > "$VS_SERVER_KEYS_PATH"; chmod 600 "$VS_SERVER_KEYS_PATH"; }

# --- свой HOME: иначе кабинет подтянет ЧУЖИЕ воркеры --------------------------------
# `server/main.py` добавляет в путь поиска `~/VideoStudio/src`. Под root это каталог
# боевой системы, то есть студия запускала бы её воркеры вместо своих. Поэтому HOME
# свой, а внутри — ссылка на наши.
[ -e "$ROOT/home/VideoStudio/src" ] || ln -s "$APP/workers" "$ROOT/home/VideoStudio/src"
export HOME="$ROOT/home"

# --- соседи внутри одного процесса ---------------------------------------------------
export VS_REWRITE_BASE="${VS_REWRITE_BASE:-http://127.0.0.1:$PORT/rewrite}"
export VS_COVER_BASE="${VS_COVER_BASE:-http://127.0.0.1:$PORT/cover}"
export VS_CABINET_BASE="${VS_CABINET_BASE:-http://127.0.0.1:$PORT}"

# --- фон включён: без него нет автовыпуска -------------------------------------------
# Пустое значение = движок выпусков и очередь работают. Именно это отличает боевой
# запуск от стенда, где фон по умолчанию выключен.
export VS_SERVER_DISABLE_BACKGROUND="${VS_SERVER_DISABLE_BACKGROUND-}"

# --- ключи, выводимые из файла ключей (та же логика, что в dev/run.sh) ---------------
_altkey="$("$PY" - "$VS_SERVER_KEYS_PATH" <<'ALTK'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
v = data.get("googler") or ""
if isinstance(v, list):
    v = v[0] if v else ""
print(v if isinstance(v, str) else "")
ALTK
)"
if [ -n "$_altkey" ]; then
  # ── Чат движка картинок (02.10.2026) ────────────────────────────────────────
  # Предварительные анализы движка (visual_context, semantic_beats) ходят НЕ через
  # канал промптов, а прямо в чат, и адрес у них был прибит к api.openai.com. Счёт
  # OpenAI пуст, поэтому прогон падал на первом же анализе, а за ним обрывался этап
  # картинок — и выглядело это как поломка у нас, хотя канал промптов был настроен
  # верно (с 02.09 только Codex).
  #
  # Уводим на тот же OpenAI-совместимый чат FastGen, которым уже пишет рерайт: он
  # оплачен и расходует ОТДЕЛЬНУЮ квоту prompt.tokens.hour, не кредиты картинок.
  # Ключ сюда НЕ кладём: его выдаёт сервер из семейства `googler` при вызове воркера —
  # одна копия секрета, в файле ключей.
  # ВЫКЛЮЧЕНО 02.10.2026, в тот же вечер, по итогам живой проверки. Деньги FastGen
  # решает, схему — нет. Задача 154 на FastGen дала 23 отказа схемы из 50 попыток и
  # умерла («semantic v4 block: не массив units»), тогда как на прежнем адресе соседние
  # задачи давали 0–2 из 12–53 и доходили. Причина названа в нашем же коде, строкой
  # 2660: сторонний OpenAI-совместимый прокси уважает `response_format` НЕ на всех
  # моделях и возвращает свободный текст. Планировщику сцен нужен строгий JSON — значит
  # сначала надо передавать схему ещё и текстовой директивой (приём уже есть в
  # `_alt_llm_call`), и только потом включать это обратно.
  #
  # Включается одной переменной, когда директива будет сделана:
  #   VS_GPT_CHAT_URL=https://api.fast-gen.ai/v1/chat/completions
  if [ -n "${VS_GPT_CHAT_URL:-}" ]; then
    export VS_GPT_CHAT_URL
    case "$VS_GPT_CHAT_URL" in
      *fast-gen.ai*)
        # Модель переставляется ВМЕСТЕ с адресом: `gpt-4.1-nano` у FastGen не
        # существует, живой вызов отвечает 400 «No supported model found» — и это
        # читается как поломка прогона, а не как неверная настройка.
        export VS_GPT_MODEL="${VS_GPT_MODEL:-gpt-4.1-mini}"
        export VS_SCENE_GPT_MODEL="${VS_SCENE_GPT_MODEL:-gpt-4.1-mini}"
        ;;
    esac
  fi
  export REWRITE_GPT_CHAT_URL="${REWRITE_GPT_CHAT_URL:-https://api.fast-gen.ai/v1/chat/completions}"
  export REWRITE_GPT_CHAT_KEY="${REWRITE_GPT_CHAT_KEY:-$_altkey}"
  case "$REWRITE_GPT_CHAT_URL" in
    *fast-gen.ai*)
      # У FastGen нет gpt-4.1-nano: живой вызов отвечает 400. Список обязан совпадать
      # с тем, что у провайдера есть, иначе интерфейс предлагает неработающее.
      export REWRITE_ALLOWED_OPENAI_MODELS="${REWRITE_ALLOWED_OPENAI_MODELS:-gpt-4.1-mini,gpt-4o-mini}"
      ;;
  esac
fi
_coverkey="$("$PY" - "$VS_SERVER_KEYS_PATH" <<'CALT'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
v = data.get("alt_llm") or ""
print(v if isinstance(v, str) else "")
CALT
)"
if [ -n "$_coverkey" ]; then
  [ -z "${COVER_ALT_API_KEY:-}" ] && export COVER_ALT_API_KEY="$_coverkey"
  export COVER_ALT_BASE_URL="${COVER_ALT_BASE_URL:-https://triklz27.ru/v1}"
  export COVER_ALT_MODEL="${COVER_ALT_MODEL:-gpt-5.6-terra}"
  export VS_ALT_LLM_KEY="${VS_ALT_LLM_KEY:-$_coverkey}"
fi
unset _altkey _coverkey

cd "$APP"
exec "$ROOT/.venv/bin/uvicorn" web.platform:app --host 127.0.0.1 --port "$PORT" \
     --no-access-log --timeout-keep-alive 65

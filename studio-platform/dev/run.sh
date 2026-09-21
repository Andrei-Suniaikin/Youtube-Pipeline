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
# Запас файловых ручек (21.09.2026). В ночь перед показом стенд умер с
# «[Errno 24] Too many open files»: соседи открывали SQLite идиомой
# `with sqlite3.connect(...)`, которая соединение НЕ закрывает, и за полчаса
# накопилось 12 599 ручек. Причина устранена (tests/test_sqlite_fd_leak.py),
# но запас оставляем: macOS по умолчанию даёт процессу мало, а платформа —
# это три сервиса в одном процессе, то есть три бюджета ручек в одном.
ulimit -n 8192 2>/dev/null || true
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${VS_PORT:-8080}"
# TLS (20.09.2026). Python с python.org НЕ берёт сертификаты из системной связки macOS:
# ssl.get_default_verify_paths() отдаёт пустоту, и КАЖДЫЙ https-вызов падает с
# CERTIFICATE_VERIFY_FAILED. Наружу это выходит как «сеть недоступна (E-NET)» — и выглядит
# как проблема интернета, хотя curl в том же терминале отвечает 200: curl берёт связку
# системы, а Python нет. На этом встали все заказы рерайта: три попытки по десять
# повторов, полчаса работы и ноль текста. Берём связку certifi из .venv; заданное снаружи
# значение не трогаем — на боевой машине связка своя.
if [ -z "$SSL_CERT_FILE" ]; then
  _CA="$("$PY" -c 'import certifi;print(certifi.where())' 2>/dev/null || true)"
  if [ -n "$_CA" ] && [ -f "$_CA" ]; then
    export SSL_CERT_FILE="$_CA"
    export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$_CA}"
  else
    echo "  ВНИМАНИЕ: не нашёл связку сертификатов (certifi) — https-вызовы могут падать"
    echo "  как «сеть недоступна». Лечится: .venv/bin/pip install certifi"
  fi
fi
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
# Ключи провайдеров ищем ДО подмены HOME (20.09.2026). Кабинет читает их из
# `~/video_studio_api_keys.json`, а строкой ниже HOME становится dev/home — то есть
# стенд искал ключи в изолированном каталоге, где их нет и не будет, и молча работал
# совсем без ключей: список моделей пуст, шаблоны Lumean «ключ не задан», статус FastGen
# «нет данных». Диагноз при этом выглядел как «сломались ключи», а сломан был путь.
# `dev/run-live.sh` делает то же самое и поэтому работает — здесь этого просто не было.
export VS_SERVER_KEYS_PATH="${VS_SERVER_KEYS_PATH:-$HOME/video_studio_api_keys.json}"
if [ ! -f "$VS_SERVER_KEYS_PATH" ]; then
  echo "  ключей нет: $VS_SERVER_KEYS_PATH — стенд поднимется, но провайдеры будут пусты"
  echo "  (свой путь: VS_SERVER_KEYS_PATH=/путь dev/run.sh)"
fi
export HOME="$ROOT/dev/home"
# Базу можно подменить снаружи: стенды поднимаются на КОПИИ базы (правило проекта),
# и раньше жёсткое присваивание перебивало переданный путь — стенд молча работал на
# общей dev-базе, а тесты смотрели в копию.
export VS_SERVER_DB="${VS_SERVER_DB:-$ROOT/dev/vsgen.dev.db}"
export VS_DEV_DB="${VS_DEV_DB:-$VS_SERVER_DB}"
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
# Ключи обложкам (20.09.2026). Cover Studio с 18.09 не принимает ключ в запросе вовсе
# («Vendor API keys больше не принимаются: используются только серверные ключи») и читает
# СВОИ переменные COVER_*. Стенд их не задавал ни одной — и анализ превью донора молча
# оставался без ключа, хотя тот же ключ лежал в общем файле и кабинетом использовался.
# Берём из общего файла, чтобы ключ жил в ОДНОМ месте: две копии расходятся молча, а
# расходятся они всегда на платном вызове. Заданное снаружи значение не трогаем.
if [ -f "$VS_SERVER_KEYS_PATH" ]; then
  for _pair in "COVER_OPENAI_KEY:openai" "COVER_FAST_GEN_KEY:googler" "COVER_VEO_KEY:veo"; do
    _var="${_pair%%:*}"; _fam="${_pair##*:}"
    if [ -z "$(eval "printf '%s' \"\$$_var\"")" ]; then
      _val="$("$PY" - "$VS_SERVER_KEYS_PATH" "$_fam" <<'KEYS'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
value = data.get(sys.argv[2])
if isinstance(value, list):          # у семейства бывает пул — обложкам отдаём первый
    value = value[0] if value else ""
if isinstance(value, dict):
    value = value.get("value") or value.get("key") or ""
print(value if isinstance(value, str) else "")
KEYS
)"
      [ -n "$_val" ] && export "$_var=$_val"
    fi
  done
  unset _pair _var _fam _val
fi

# ─── Чем писать промпты к кадрам (21.09.2026) ────────────────────────────────
# У счёта OpenAI ноль: провайдер отвечает 429 insufficient_quota / «You have no credits
# remaining». Повтор при этом не помогает никогда, и весь конвейер встаёт на промптах.
#
# Оказалось, что у FastGen — того самого провайдера, за которого уже заплачено и который
# рисует нам кадры, — есть полноценный OpenAI-совместимый /v1/chat/completions: 28 моделей
# (GPT-5.x/4.x/o-серия, Grok, Gemini), наш заголовок Authorization: Bearer принимается,
# ответ приходит штатной chat.completion. Проверено живым вызовом с подставным словом в
# запросе: модель вернула именно его, то есть это настоящая генерация, а не заглушка.
# Расход идёт из ОТДЕЛЬНОЙ квоты prompt.tokens.hour (200 000 токенов/час) — кредиты картинок
# она не трогает.
#
# Подключается это существующим каналом altclaude, без единой правки движка. Две тонкости,
# каждая проверена, а не выведена из названий:
#   * база ОБЯЗАНА включать /v1: адрес склеивается как base + "/chat/completions" без
#     нормализации, и без префикса получается 404, который считается фатальным;
#   * модель ОБЯЗАНА быть в стиле OpenRouter (openai/...). Дефолт канала — claude-sonnet-5,
#     а моделей Anthropic на этом эндпоинте нет вовсе: любой запрос вернул бы 400.
# Ключ тот же, что у картинок (семейство googler) — второй копии секрета не заводим.
# Заданное снаружи не трогаем: VS_PROMPT_PROVIDER=openai вернёт прежнее поведение.
if [ -f "$VS_SERVER_KEYS_PATH" ] && [ -z "$VS_ALT_LLM_KEY" ]; then
  # КЛЮЧ ПРИНАДЛЕЖИТ ХОСТУ. Здесь берётся ключ FastGen (семейство googler) — и только он,
  # потому что ниже адресом стоит именно FastGen. Раньше тут было `alt_llm or googler`, и
  # пока alt_llm в файле не было, подставлялся googler и всё сходилось. 21.09 alt_llm
  # появился (он нужен обложкам, у которых ДРУГОЙ хост) — и молча победил: на FastGen
  # поехал ключ чужого провайдера, а рерайт отвечал «Неверный API ключ», то есть 401,
  # который читается как «сломался аккаунт», хотя сломалось сопоставление ключа и адреса.
  # Никаких «или»: у каждого маршрута своё семейство, иначе добавление любого нового
  # ключа снова тихо переставит чужой.
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
    export VS_ALT_LLM_KEY="$_altkey"
    export VS_ALT_LLM_BASE="${VS_ALT_LLM_BASE:-https://api.fast-gen.ai/v1}"
    export VS_ALT_LLM_MODEL="${VS_ALT_LLM_MODEL:-openai/gpt-4.1-mini}"
    export VS_PROMPT_PROVIDER="${VS_PROMPT_PROVIDER:-altclaude}"
    # Тот же провайдер пишет и ТЕКСТ. Рерайт ходит в чат одним местом, адрес и ключ у него
    # теперь настраиваются; адрес задаётся целиком — склейка с /v1 даёт 404. Имена моделей
    # рерайта (gpt-4.1-mini и т.п.) провайдер принимает и без префикса — проверено.
    # Ключ ставится ВМЕСТЕ с адресом: без него рерайт откажется работать, чтобы не
    # отправить ключ OpenAI чужому провайдеру.
    export REWRITE_GPT_CHAT_URL="${REWRITE_GPT_CHAT_URL:-https://api.fast-gen.ai/v1/chat/completions}"
    export REWRITE_GPT_CHAT_KEY="${REWRITE_GPT_CHAT_KEY:-$_altkey}"
  fi
  # Анализ обложки. В этом форке он ходит ТОЛЬКО в OpenAI: Gemini-SDK удалён в v1.0.3,
  # provider="google" молча падает обратно на OpenAI. Без денег на счету обложка «новой
  # сценой» невозможна в принципе. Бой решает это теми же COVER_ALT_* — сторонним
  # OpenAI-совместимым прокси, который умеет зрение (проверено живым вызовом: прочитал
  # текст с картинки). Модель берётся ЕГО, потому что моделей OpenAI у него нет.
  # Ключ ставится вместе с адресом: без него анализ откажется работать, чтобы не
  # отправить ключ OpenAI чужому провайдеру. Канал узкий — в бою COVER_ALT_INFLIGHT=1.
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
  if [ -n "$_coverkey" ] && [ -z "$COVER_ALT_API_KEY" ]; then
    export COVER_ALT_API_KEY="$_coverkey"
    export COVER_ALT_BASE_URL="${COVER_ALT_BASE_URL:-https://triklz27.ru/v1}"
    export COVER_ALT_MODEL="${COVER_ALT_MODEL:-gpt-5.6-terra}"
  fi
  unset _coverkey
  unset _altkey
fi
# ─── Пресеты рерайта: стенд не пишет в репозиторий ───────────────────────────
# rewrite/server.py держит пресеты тенантов в REWRITE_PRESETS_DIR, а по умолчанию —
# прямо в рабочем дереве (rewrite/presets/<тенант>/generation.json). Переменную никто
# не задавал, поэтому каждый прогон стенда правил ОТСЛЕЖИВАЕМЫЙ файл: одна генерация
# — и в git diff висит чужое «updated_at». Пресеты стенда — это состояние, а не
# исходник, и живут они рядом с dev-базой, как всё остальное состояние.
export REWRITE_PRESETS_DIR="${REWRITE_PRESETS_DIR:-$ROOT/dev/data/rewrite-presets}"
# Готовые обложки — тоже состояние, а не исходник. Cover Studio по умолчанию кладёт их в
# СВОЙ каталог модуля (cover/cover_server.py: OUTPUT_DIR = BASE_DIR/"outputs"), то есть
# прямо в дерево репозитория: владелец 21.09 так и сказал — «записал в корень проекта».
# Уводим туда же, где живёт остальное состояние стенда. Старые задачи не пострадают:
# путь лежит абсолютным значением в самой строке задачи и читается оттуда.
export COVER_OUTPUT_DIR="${COVER_OUTPUT_DIR:-$ROOT/dev/data/cover-outputs}"
# Первый запуск: каталога ещё нет и пресетов в нём тоже, а стенд без них показывает
# пустой список. Стартовый набор берётся из репозитория ОДИН раз — дальше стенд живёт
# своей копией и в дерево уже не возвращается. Нужен чистый лист — удалить каталог.
if [ -z "$(ls -A "$REWRITE_PRESETS_DIR" 2>/dev/null)" ]; then
  mkdir -p "$REWRITE_PRESETS_DIR"
  if [ -d "$ROOT/rewrite/presets" ]; then
    cp -R "$ROOT/rewrite/presets/." "$REWRITE_PRESETS_DIR/"
  fi
fi
# Рендер делает отдельная машина, опрашивая /api/render/claim с этим токеном. Пустой
# токен сервер отвергает всегда, поэтому локально даётся значение по умолчанию — то же,
# что берёт dev/render-node.sh. В бою токен настоящий и живёт в .env.
export PULLER_TOKEN="${PULLER_TOKEN:-dev-render-node}"
# Субтитры YouTube. На VPS они идут через туннель (облачные адреса YouTube не обслуживает),
# на домашней машине — напрямую. Без этого значения стенд пытался бы лезть в туннель,
# которого здесь нет, и отвечал «Missing dependencies for SOCKS support».
export VS_SUBS_PROXIES="${VS_SUBS_PROXIES:-direct}"
# ─── Общий на машину учёт мест под ФИЗИЧЕСКИЙ ключ (приехал с ребейзом 18.09) ───
# Боевой rewrite_core.py импортирует credential_budget БЕЗ try. Нет модуля — не
# поднимается вся платформа: у нас это один процесс на три сервиса.
export VIDEO_STUDIO_SHARED_DIR="${VIDEO_STUDIO_SHARED_DIR:-$ROOT/shared}"
# Замки — это файлы, и каталог обязан быть приватным (0700, наш uid): модуль это
# проверяет и иначе падает BudgetConfigurationError на ПЕРВОМ платном заказе, а не
# на старте. В бою каталог лежит в /run на tmpfs (пережить перезагрузку не должен,
# пережить перезапуск сервиса — обязан); на macOS /run нет, поэтому кладём рядом с
# остальным состоянием стенда.
export VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR="${VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR:-$ROOT/dev/data/credential-budget}"
mkdir -p "$VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR" && chmod 700 "$VIDEO_STUDIO_CREDENTIAL_BUDGET_DIR"
# Потолки одновременных вызовов на один физический ключ. Умолчания — БОЕВЫЕ
# (fastgen 18, openai 12, veo 24, claude_oauth 1, codex 1), поэтому здесь ничего не
# задаётся. Знать про них надо вот что: боевой claude_oauth=1 СТРОЖЕ нашего
# сегодняшнего поведения — форк 31.08 разрешал три параллельных вызова на токен
# (REWRITE_CLAUDE_PER_TOKEN_CONCURRENCY=3). Решение «оставить как в бою или вернуть
# три» — владельца, поэтому ручка есть, а умолчание не трогаем:
#   export VIDEO_STUDIO_CREDENTIAL_BUDGET_CLAUDE_OAUTH=3
# Значение задаётся на МАШИНУ целиком: разные числа в разных процессах ломают общий счёт.

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

# Уборка диска — тоже отдельным процессом (в бою systemd-таймер). Крутится всегда, когда
# задан срок хранения: без VS_RETENTION_AUTO проход только считает и пишет отчёт для
# экрана «Админ → Диск», ничего не удаляя.
if grep -qE '^VS_RETENTION_(STRIP|DROP)_DAYS=[0-9]+' "$ROOT/.env" 2>/dev/null; then
  ( cd "$ROOT" && "$PY" dev/retention.py --loop ) &
  if grep -qE '^VS_RETENTION_AUTO=(1|yes|on|true)' "$ROOT/.env" 2>/dev/null; then
    CLEAN="  уборка  ВКЛЮЧЕНА: старые проекты будут удаляться сами"
  else
    CLEAN="  уборка  только считает: нет VS_RETENTION_AUTO=1 (см. Админ → Диск)"
  fi
else
  CLEAN="  уборка  срок хранения не задан: VS_RETENTION_STRIP_DAYS / _DROP_DAYS в .env"
fi

echo
echo "  пульт    http://127.0.0.1:$PORT/studio/    (новый единый интерфейс)"
echo "  кабинет  http://127.0.0.1:$PORT/           (боевой VSGen, пути прежние)"
echo "  rewrite  http://127.0.0.1:$PORT/rewrite/"
echo "  cover    http://127.0.0.1:$PORT/cover/"
echo "  что смонтировано: http://127.0.0.1:$PORT/_platform"
echo "$ALERTS"
echo "$CLEAN"
echo "  рендер   отдельной машиной: dev/render-node.sh во втором окне"
echo
echo "  вход — логин и пароль кабинета (dev / VS_DEV_PASSWORD)."
echo "  ключи соседей и пароль живут в базе; после dev/make_db.py их нужно выдать заново:"
echo "    .venv/bin/python dev/provision.py"
echo
wait

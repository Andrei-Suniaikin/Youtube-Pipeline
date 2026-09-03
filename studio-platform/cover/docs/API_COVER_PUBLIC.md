# Cover Generator API v1 — документация для интеграции

> Версия: 1.1.0
> Базовый URL: `https://cover.example.invalid`
> Swagger UI (интерактивно): `https://cover.example.invalid/docs`
>
> ⚠️ **Миграция 2026-07-17:** сервис переехал. Старый базовый URL
> `https://legacy-cover.example.invalid` продолжает работать через
> прокси-мост (переходный период), но новые интеграции делайте на новом
> https-адресе — старый будет отключён после уведомления.

REST API для программной генерации обложек YouTube. Принимает ссылку на видео
и языки, ставит job в честную очередь и возвращает готовые JPEG-обложки.
Vendor API keys настраиваются только администратором сервера.

---

## Что нужно для интеграции

| # | Что | Откуда |
|---|------|--------|
| 1 | URL базы | См. выше |
| 2 | API ключ `cvr_xxx` | Выдаст администратор |
| 3 | Имя пресета (`preset_name`) | Получишь через `GET /api/v1/presets` |

**Не нужно**: ничего устанавливать, никаких SDK. Любой HTTP-клиент (curl, httpx, axios, fetch).

---

## Quickstart (3 шага до первой обложки)

### 1. Узнай доступные пресеты
```bash
curl https://cover.example.invalid/api/v1/presets
```
Ответ: список `{name, languages}`. Выбери пресет — он определяет визуальный стиль, шрифты, поддерживаемые языки.

### 2. (Опционально) Загрузи референс стиля
```bash
KEY="cvr_xxxxxxxxxxxxxxxxxxxxxxx"

curl -X POST https://cover.example.invalid/api/v1/files \
  -H "Authorization: Bearer $KEY" \
  -F "file=@reference.jpg"
# {"file_id": "cI_rO_pv", "filename": "reference.jpg", "size": 245760}
```

### 3. Создай job
```bash
curl -X POST https://cover.example.invalid/api/v1/jobs \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://youtube.com/watch?v=DEMO0000001"],
    "preset_name": "Default RU",
    "languages": ["Russian", "English"],
    "refs_per_language": {"Russian": "cI_rO_pv"}
  }'
# {"job_id": "a8f3b2c1d4e5f607", "status": "pending"}
```

> Не передавайте OpenAI/FastGen/Veo credentials. Сервер использует только
> собственные ключи; отсутствие нужного server key возвращает `503`.

Опрашивай `GET /api/v1/jobs/{job_id}` каждые 15-30 секунд до `status="completed"`. Скачивай через `GET /api/v1/jobs/{job_id}/files/{lang_code}`.

Полный end-to-end пример см. ниже в разделе [Examples](#examples).

---

## Authentication

### Bearer (рекомендуется, RFC 6750)
```http
Authorization: Bearer cvr_xxxxxxxx
```

### X-API-Key (legacy)
```http
X-API-Key: cvr_xxxxxxxx
```

### Plain в Authorization (convenience)
```http
Authorization: cvr_xxxxxxxx
```

Приоритет при наличии нескольких: Authorization → X-API-Key.

### Формат ключа

`cvr_` + 28 url-safe символов = 190 bit entropy.

### Жизненный цикл

| Состояние | Поведение |
|-----------|-----------|
| `active` | 200 OK на запросы |
| `revoked` | 401 на следующий же запрос |

Ключи не удаляются физически (soft delete) — для аудита. Если ключ скомпрометирован, попроси администратора отозвать и выдать новый.

---

## Endpoints

### `GET /api/v1/presets`
Public. Список доступных пресетов с языками.

```bash
curl https://cover.example.invalid/api/v1/presets
```

Response 200 — **dict** `{preset_name: PresetInfo}` (не массив):
```json
{
  "Default RU": {
    "name": "Default RU",
    "languages": ["Русский", "Английский"],
    "language": "Русский",
    "generation_provider": "flow",
    "generation_model": "GEM_PIX_2",
    "fallback_mode": "auto"
  },
  "My Channel RO": {
    "name": "My Channel RO",
    "languages": ["Румынский", "Чешский"],
    "language": "Румынский",
    "generation_provider": "flow",
    "generation_model": "GEM_PIX_2",
    "fallback_mode": "auto"
  }
}
```

> **Внимание**: имена пресетов и состав языков зависят от инсталляции и могут меняться. **Не хардкодь** имена — получай через этот endpoint. `languages` возвращаются **русскими названиями** (canonical в системе), но POST `/jobs` принимает любую из 3 форм (см. ниже).

Discover-pattern в коде:
```python
presets = cli.get("/api/v1/presets").json()
preset_name = next(iter(presets))                  # первый доступный
# или: preset_name = "Default RU" if "Default RU" in presets else next(iter(presets))
languages = presets[preset_name]["languages"][:1]  # первый язык пресета
```

---

### `GET /api/v1/presets/{name}`
Полная структура одного пресета (все поля).

```bash
curl -H "Authorization: Bearer $KEY" \
  https://cover.example.invalid/api/v1/presets/Default%20RU
```
Response 200: полный объект Preset со всеми полями (см. поля в POST). 404 если нет.

---

### `POST /api/v1/presets`
Создать новый пресет.

```bash
curl -X POST https://cover.example.invalid/api/v1/presets \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "MyPreset",
    "language": "Русский",
    "languages": "Русский,Английский",
    "generation_provider": "flow",
    "generation_model": "GEM_PIX_2",
    "fallback_mode": "auto"
  }'
```

**Поля** (все опциональные кроме `name`; используются дефолты Preset()):

| Поле | Тип | По умолчанию | Допустимые значения |
|------|-----|--------------|---------------------|
| `name` | `str` | — обязательно | уникально в рамках user_id, 1-80 chars |
| `language` | `str` | `"Русский"` | русское название из списка языков |
| `languages` | `str` | `""` | CSV русских названий, напр. `"Русский,Английский"` |
| `style_prompt` | `str` | `""` | используется если `use_auto_style=false` |
| `use_auto_style` | `bool` | `true` | анализ стиля через ИИ |
| `simple_mode` | `bool` | `false` | ⚡ img2img: превью редактируется моделью (сама переводит текст + слегка меняет позы), без vision-анализа. Только url-mode |
| `reference_image` | `str` | `""` | 🎨 `file_id` референса стиля (из `POST /api/v1/files`): двухкартиночный style-transfer — смысл/текст из обложки, стиль из референса. Только url-mode |
| `fallback_mode` | `str` | `"auto"` | `auto` / `always_overlay` (= пропустить проверку текста на результате) |
| `generation_provider` | `str` | `"flow"` | `flow` / `grok` / `openai_img` / `flower` |
| `generation_model` | `str` | `"GEM_PIX_2"` | для `flow`: `GEM_PIX_2` (Nano Banana Pro) / `NARWHAL` (Nano Banana 2); для `grok`: `quality`/`speed`; для остальных `""` |
| `analysis_provider` | `str` | `"openai"` | `openai` (значения `google`/`gemini` auto-migrated → `openai`) |
| `analysis_model` | `str` | `"gpt-4.1-mini"` | OpenAI model name |
| `aspect_ratio` | `str` | `"16:9"` | |
| `max_generation_retries` | `int` | `5` | 1-20 |
| `debug_mode` | `bool` | `false` | подробные логи |

> Поля `font_path`, `text_color`, `outline_*`, `text_position`, `text_size_pct`,
> `fast_gen_base_url` из старых версий доки удалены: они не влияли на генерацию.
> Если ваш клиент их присылает — они молча игнорируются (не 422).

Response **201**: полный объект пресета (с заполненными `created_at`, `updated_at`).

**Errors**:
- `422` — имя уже существует, неверный provider/model, неверный язык в `languages`, неверная позиция/fallback_mode

---

### `PUT /api/v1/presets/{name}`
Полная замена пресета. Имя в URL должно совпадать с `body.name` (переименование запрещено — используй DELETE+POST).

```bash
curl -X PUT https://cover.example.invalid/api/v1/presets/MyPreset \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"MyPreset","language":"Русский","languages":"Русский","generation_provider":"flow","generation_model":"GEM_PIX_2","fallback_mode":"auto"}'
```

Response **200**: обновлённый пресет. Поле `created_at` сохраняется, `updated_at` — текущее время.

**Errors**:
- `404` — пресета не существует
- `422` — `body.name` ≠ URL `{name}`; те же что у POST

---

### `DELETE /api/v1/presets/{name}`
Удалить пресет.

```bash
curl -X DELETE -H "Authorization: Bearer $KEY" \
  https://cover.example.invalid/api/v1/presets/MyPreset
```

Response **204** No Content.

**Errors**:
- `404` — пресета нет
- `409` — у пользователя есть активный job (`pending` или `running`) использующий этот пресет. Сначала дождаться завершения или отменить job.

---

### `POST /api/v1/files`
Upload референса стиля (JPG/PNG/WEBP).

```bash
curl -X POST https://cover.example.invalid/api/v1/files \
  -H "Authorization: Bearer $KEY" \
  -F "file=@reference.jpg"
```

Response 201:
```json
{
  "file_id": "cI_rO_pv",
  "filename": "reference.jpg",
  "size": 245760
}
```

**Лимиты**:
- Форматы: JPG/PNG/WEBP/GIF/BMP — сервер проверяет magic-bytes; не-картинка → 415
- `file_id`: 8-символьный, видимый только тебе

**Errors**: 401, 422 (пустой запрос / missing file field).

---

### `POST /api/v1/jobs`
Создать job. Сразу в очередь, возвращает `job_id`.

```bash
curl -X POST https://cover.example.invalid/api/v1/jobs \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://youtube.com/watch?v=DEMO0000001"],
    "preset_name": "Default RU",
    "languages": ["Russian", "English"],
    "refs_per_language": {"Russian": "cI_rO_pv"}
  }'
```

**Поля запроса**:

| Поле | Тип | Обязательно | Описание |
|------|-----|-------------|----------|
| `urls` | `List[str]` | ✓ | 1-50 YouTube ссылок |
| `preset_name` | `str` | ✓ | Имя из `GET /presets` |
| `languages` | `List[str]` | ✓ | 1-10 языков (русские названия или ISO коды: `Russian`, `ru`, `Английский`, `en`) |
| `refs_per_language` | `Dict[str, str]` | — | Маппинг `язык → file_id`. Можно не для всех языков. |

Поля `openai_api_key`, `fast_gen_api_key`, `veo_api_key`, `max_threads` и
`parallel` не входят в публичный контракт. Непустой vendor key отклоняется с
`422`; число потоков и повторов задаётся только сервером.

Response 201:
```json
{
  "job_id": "a8f3b2c1d4e5f607",
  "status": "pending"
}
```

**Validation errors** (422):
- `urls=[]` → "List should have at least 1 item"
- `preset_name="ghost"` → "preset 'ghost' not found"
- `languages=["Klingon"]` → "unsupported language: 'Klingon'. Use canonical name or ISO code"
- `refs_per_language={"Russian": "nope"}` → "file_id 'nope' not found"

---

### Title mode (v1.0.4)

Альтернатива URL-режиму: вместо YouTube-ссылок передаёшь **название** на каждом языке + опционально `extra_prompt`. Без анализа сцены — GPT придумывает её из тайтла.

**Когда использовать:**
- Видео ещё не загружено на YouTube
- Хочешь обложку под конкретный сценарий не от существующего ролика
- Юзер сам контролирует текст на каждом языке (никакой автоадаптации через LLM)

**Поля mode=title:**

| Поле | Тип | Обязательно | Описание |
|------|-----|-------------|----------|
| `mode` | `"title"` | ✓ | Включает title-режим |
| `titles_per_lang` | `Dict[str, str]` | ✓ | `{язык: текст обложки на этом языке}`. **Ключи должны точно совпадать с `languages`** (после нормализации). **Пустой `value` разрешён** (v1.0.7): обложка для этого языка генерируется **без текста** — только визуал/фон (полезно для шаблонов). |
| `extra_prompt` | `str` | — | Доп.детали сцены (max 2000 chars). На любом языке — GPT понимает, но в сцене выдаст по-английски для Fast Gen. |
| `urls` | — | ✗ | Запрещено в title-mode (422 если передано) |

**Пример curl:**
```bash
curl -X POST https://cover.example.invalid/api/v1/jobs \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "title",
    "titles_per_lang": {
      "Русский": "10 САМЫХ СТРАШНЫХ МЕСТ",
      "Английский": "TOP 10 SCARIEST PLACES"
    },
    "extra_prompt": "Тёмный лес ночью, туман, заброшенный дом, полная луна",
    "preset_name": "Default RU",
    "languages": ["Русский", "Английский"]
  }'
# {"job_id": "...", "status": "pending"}
```

**Пример с no-text обложкой (v1.0.7):** одна обложка с текстом, две без — для шаблонов/фонов:
```bash
curl -X POST https://cover.example.invalid/api/v1/jobs \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "mode": "title",
    "titles_per_lang": {
      "Русский": "",
      "Английский": "TEST EMPTY",
      "Испанский": ""
    },
    "extra_prompt": "яркий пейзаж океан скалы",
    "preset_name": "Default RU",
    "languages": ["Русский", "Английский", "Испанский"]
  }'
# ru/es сгенерируются без текста (чистый фон), en — с текстом "TEST EMPTY"
```

**Что внутри:**
1. GPT (по `preset.analysis_model`) генерирует **одну** сцену из `base_title + extra_prompt` (base_title = `titles_per_lang[preset.language]` или первый ключ). Сцена всегда на английском.
2. Для каждого языка: текст из `titles_per_lang[lang]` идёт в обложку **как есть** (без LLM-адаптации — юзер уже на нужном языке ввёл).
3. `progress_by_language` и `/files/{lang_code}` работают как в URL-mode.
4. Файлы лежат в `<safe_base_title>/<lang_code>_…_<preset>.jpg`. Если sanitize даёт пусто (тайтл из эмодзи/символов) — fallback `untitled_<sha256:6>`.

**Validation errors (422):**
- `mode=title` без `titles_per_lang` → "titles_per_lang required for mode=title"
- `mode=title` с `urls` → "urls only allowed for mode=url"
- Ключи `titles_per_lang` ≠ `languages` → "titles_per_lang keys must match languages exactly"
- `extra_prompt` длиннее 2000 → "extra_prompt too long"

> Пустое значение `titles_per_lang[lang]` — **валидно** (v1.0.7): обложка для этого
> языка генерируется без текста (фон/шаблон).

---

### `GET /api/v1/jobs`
Список твоих jobs.

```bash
curl "https://cover.example.invalid/api/v1/jobs?limit=20&offset=0&status=completed" \
  -H "Authorization: Bearer $KEY"
```

**Query**:
- `limit` (1-100, default 20)
- `offset` (default 0)
- `status` (опционально: `pending`, `running`, `completed`, `completed_partial`, `failed`, `cancelled`)

Response 200:
```json
{
  "items": [
    {
      "id": "a8f3b2c1d4e5f607",
      "status": "completed",
      "created_at": 1747749600.0,
      "started_at": 1747749601.5,
      "finished_at": 1747749688.2,
      "progress_percent": 100,
      "progress_detail": "1/1 обложек",
      "results_count": 1
    }
  ],
  "total": 7
}
```

---

### `GET /api/v1/jobs/{job_id}`
Детали + per-language прогресс.

```bash
curl https://cover.example.invalid/api/v1/jobs/a8f3b2c1d4e5f607 \
  -H "Authorization: Bearer $KEY"
```

Response 200:
```json
{
  "id": "a8f3b2c1d4e5f607",
  "status": "running",
  "created_at": 1747749600.0,
  "started_at": 1747749601.5,
  "finished_at": 0.0,
  "progress_percent": 50,
  "progress_detail": "1/2 обложек",
  "progress_by_language": {
    "ru": {"code": "ru", "display": "Русский",    "done": 1, "total": 1, "fallback_count": 0, "files": ["Rick_Astley_…/ru_…jpg"], "status": "completed"},
    "en": {"code": "en", "display": "Английский", "done": 0, "total": 1, "fallback_count": 0, "files": [],                          "status": "pending"}
  },
  "results_count": 1,
  "output_files": ["Rick_Astley.../ru_DEMO0000001.jpg"],
  "fallback_files": [],
  "error": ""
}
```

**`progress_by_language`** — поля каждого языка:

| Поле | Тип | Описание |
|------|-----|----------|
| `code` | `str` | ISO-код (`ru`, `en`, ...) — используется в URL `/files/{lang_code}` |
| `display` | `str` | Русское название (для UI) |
| `done` | `int` | Сколько обложек готово |
| `total` | `int` | Сколько должно быть (== числу `urls` в job) |
| `fallback_count` | `int` | Из `done` — сколько через программный fallback-overlay |
| `files` | `List[str]` | Относительные пути готовых файлов |
| `status` | `str` | `pending` / `partial` / `completed` (per-language, **не** статус job) |

**Errors**: 401, 403 (чужой job), 404.

---

### `DELETE /api/v1/jobs/{job_id}`
Graceful cancel. Worker остановится на следующем chunk-check.

```bash
curl -X DELETE https://cover.example.invalid/api/v1/jobs/a8f3b2c1d4e5f607 \
  -H "Authorization: Bearer $KEY"
```

Response **202 Accepted**:
```json
{"status": "cancelled", "job_id": "a8f3b2c1d4e5f607"}
```
Для pending отмена мгновенная. Running-job останавливается кооперативно: ожидание
provider-slot прерывается быстро, но уже начатый сетевой запрос может завершаться
до своего timeout. Флаг отмены хранится в SQLite и переживает рестарт сервиса.

Job не удаляется, статус становится `cancelled`. Уже готовые файлы остаются скачиваемыми.

**Errors**: 401, 403, 404, 409 (job в terminal статусе).

---

### `GET /api/v1/jobs/{job_id}/files/{lang_code}`
Скачать готовую обложку.

```bash
curl -o cover_ru.jpg \
  https://cover.example.invalid/api/v1/jobs/a8f3b2c1d4e5f607/files/ru \
  -H "Authorization: Bearer $KEY"
```

`lang_code`: короткий ISO код (`ru`, `en`, `es`, `pt`, ...). Узнать какие коды доступны для job → ключи объекта `progress_by_language`. Если по одному lang_code несколько файлов (несколько URL в job) — вернётся ZIP вместо JPEG, см. `Content-Type` ответа.

Response 200: бинарный JPEG/PNG.
Headers: `Content-Type: image/jpeg`, `Content-Disposition: attachment; filename=...`

**Errors**: 401, 403, 404 (нет файла — job не завершён или язык не в job).

---

### `POST /api/v1/jobs/{job_id}/retry?full={bool}`
Повтор. Два режима:

**`full=false` (default)** — resume: продолжить с failed/partial, не перегенерировать готовое.
```bash
curl -X POST "https://cover.example.invalid/api/v1/jobs/{id}/retry" \
  -H "Authorization: Bearer $KEY"
```

**`full=true`** — wipe + rerun с нуля.
```bash
curl -X POST "https://cover.example.invalid/api/v1/jobs/{id}/retry?full=true" \
  -H "Authorization: Bearer $KEY"
```

Response 200:
```json
{"status": "queued", "job_id": "a8f3b2c1d4e5f607"}
```

**Errors**: 401, 403, 404, 409 (job в pending/running — нечего перезапускать).

---

## Status reference

| Status | Terminal? | Retryable? | Downloadable? | Описание |
|--------|-----------|------------|---------------|----------|
| `pending` | ✗ | ✗ | ✗ | В очереди |
| `running` | ✗ | ✗ | partial | Генерация идёт |
| `completed` | ✓ | ✗ | ✓ | Все обложки готовы, без fallback |
| `completed_partial` | ✓ | ✓ (full=true) | ✓ | Готово, но часть через fallback-overlay (текст наложен программно, не AI). Это **не** ошибка — обложка качественная, просто способ другой. |
| `failed` | ✓ | ✓ | partial | См. поле `error` |
| `cancelled` | ✓ | ✓ | partial | Отменён пользователем |

---

## Error codes

| HTTP | Когда |
|------|-------|
| 400 | Невалидный body (не JSON, missing required) |
| 401 | Нет ключа, ключ невалидный, ключ revoked |
| 403 | Ключ валиден, но job чужой |
| 404 | Job/file не существует |
| 409 | Конфликт статуса (retry на pending, cancel на completed) |
| 413 | Upload файла >20MB |
| 415 | Upload не картинка |
| 422 | Validation (preset не найден, язык unsupported, и т.д.) |
| 429 | Сработал только технический pending-cap (по умолчанию 100 jobs на пользователя) |
| 503 | Нужный vendor key не настроен на сервере |
| 500 | Серверная ошибка — сообщи в support с job_id и временем |

---

## Поддерживаемые языки

POST `/api/v1/jobs` принимает любую из 3 форм (case-insensitive):
- Английское название: `English`, `Russian`, `Romanian`, `Czech`
- Русское название: `Английский`, `Русский`, `Румынский`, `Чешский`
- ISO-2: `en`, `ru`, `ro`, `cs`

Список всех 45 поддерживаемых ISO-кодов: `az ar hy be bg bs ca cs da de el en es et fa fi fr he hi hr hu id it ja ka kk ko lt lv mk nl no pl pt ro ru sk sl sr sv th tr uk uz vi zh`.

Точный список языков на пресет — `GET /api/v1/presets` → `[preset_name].languages`.

В ответах API (`progress_by_language`, URL `/files/{lang_code}`) всегда возвращается **короткий ISO-код** (поле `code`). Поле `display` рядом — для UI.

---

## Examples

### Python httpx — end-to-end
```python
import httpx, time, sys

BASE = "https://cover.example.invalid"
KEY = "cvr_xxxxxxxxxxxxxxxxxxxxxx"
HEADERS = {"Authorization": f"Bearer {KEY}"}

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=30.0) as cli:
    # 0. Discover presets/languages (не хардкодь имена!)
    presets = cli.get("/api/v1/presets").json()
    preset_name = "Default RU" if "Default RU" in presets else next(iter(presets))
    language = presets[preset_name]["languages"][0]  # canonical русское
    print(f"Using preset={preset_name} lang={language}")

    # 1. Upload reference
    with open("ref.jpg", "rb") as f:
        r = cli.post("/api/v1/files", files={"file": f})
        r.raise_for_status()
        file_id = r.json()["file_id"]

    # 2. Create job (languages принимает русское/английское/ISO в любом миксе).
    # Vendor credentials клиент не передаёт.
    r = cli.post("/api/v1/jobs", json={
        "urls": ["https://youtube.com/watch?v=DEMO0000001"],
        "preset_name": preset_name,
        "languages": [language],
        "refs_per_language": {language: file_id},
    })
    r.raise_for_status()
    job_id = r.json()["job_id"]
    print(f"Job created: {job_id}")

    # 3. Poll
    while True:
        r = cli.get(f"/api/v1/jobs/{job_id}")
        data = r.json()
        print(f"  status={data['status']} progress={data['progress_detail']}")
        if data["status"] in ("completed", "completed_partial", "failed", "cancelled"):
            break
        time.sleep(15)

    if data["status"] == "failed":
        print(f"FAILED: {data['error']}", file=sys.stderr)
        sys.exit(1)

    # 4. Download
    r = cli.get(f"/api/v1/jobs/{job_id}/files/ru")
    r.raise_for_status()
    with open("cover_ru.jpg", "wb") as f:
        f.write(r.content)
    print(f"Saved cover_ru.jpg ({len(r.content)} bytes)")
```

### Bash one-liner — submit + wait + download
```bash
KEY="cvr_xxx"
BASE="https://cover.example.invalid"

JOB=$(curl -s -X POST "$BASE/api/v1/jobs" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d @body.json | jq -r .job_id)

while true; do
  STATUS=$(curl -s "$BASE/api/v1/jobs/$JOB" -H "Authorization: Bearer $KEY" | jq -r .status)
  echo "$(date +%H:%M:%S) $STATUS"
  [[ "$STATUS" == "completed" || "$STATUS" == "completed_partial" || "$STATUS" == "failed" ]] && break
  sleep 15
done

curl -s -o cover.jpg "$BASE/api/v1/jobs/$JOB/files/ru" -H "Authorization: Bearer $KEY"
file cover.jpg
```

---

## Limits & Fair use

- **Per-user isolation**: ты видишь только свои jobs/files. Другие интеграторы изолированы.
- **`urls` максимум 50** в одном job. Больше — разбивай на отдельные jobs.
- **`languages` максимум 10** в job.
- **Fair queue**: FIFO внутри пользователя/provider-группы, round-robin между
  пользователями. Насыщенный Veo, GPT-Image-2 или FastGen не занимает slots
  другой provider-группы; новый пользователь получает ближайший доступный slot.
- **Worker concurrent jobs** задаёт сервер (`COVER_JOB_SLOTS`).
- **Upload**: до 20 МБ за файл.
- **Время генерации**: типично 60-120 сек на 1 язык. Polling рекомендуется с интервалом 15-30 сек.
- **Job retention**: 7 дней после `finished_at`, потом auto-cleanup (файлы + запись). Скачивай вовремя.

### Технические предохранители
- Тарифных и дневных квот нет.
- **Pending-cap**: по умолчанию не больше 100 ожидающих jobs на пользователя → `429`;
  значение управляется сервером.
- **Размер задачи**: ≤50 URL, ≤10 языков, ≤200 обложек на одну задачу.

### Чего пока **нет** в v1.0 (planned for v1.1+)
- **Webhooks** — только polling. Поле `webhook_url` игнорируется.
- **Формального req/min-лимита нет**, но не больше ~30 req/min на ключ. Polling интервалом 15-30 сек — нормально.
- **Idempotency-Key header** — retry POST `/jobs` после network timeout может создать дубль. Используй уникальный `urls` или проверяй `GET /jobs` перед повтором.
- **Bulk endpoints** — каждый job создаётся отдельно.

---

## Support

Если что-то сломалось / непонятно / нужна расширенная фича:

**Что прислать в отчёт о баге**:
1. HTTP метод + URL + headers (без plain key, только префикс `cvr_xxxxxxxxxxxxxxxx...`)
2. Request body (если POST)
3. Response статус + тело
4. Time (примерно, для поиска в логах сервера)
5. Шаги воспроизведения

Контакт: **<укажи telegram/email>**

---

## Changelog

**v1.1.0 (2026-07-28)** — Shared server keys + fair queue
- Vendor credentials удалены из UI и job payload; используются только server env keys.
- Дневные/free-квоты убраны.
- Один fair dispatcher: FIFO per user + work-conserving round-robin между пользователями.
- Provider-aware admission с отдельными job caps для FastGen/Veo/GPT-Image-2.
- `cancel_requested` хранится в SQLite; stale worker/restart не воскрешает отменённую job.
- Provider names проходят единый canonical policy; неизвестные и значения с пробелами отклоняются.
- `parallel`, клиентские `max_threads` и retries больше не управляют серверной мощностью.
- Legacy jobs автоматически scrub-ятся; публичная job-сериализация работает по allowlist.

> Записи ниже описывают историческое поведение старых версий. Текущий контракт
> определяется v1.1.0: vendor keys в запросах больше не принимаются.

**v1.0.7 (2026-05-22)** — Empty title → no-text cover
- В title-mode пустой `value` в `titles_per_lang["lang"] = ""` теперь разрешён → обложка генерируется **без текста** (только визуал по scene_description + extra_prompt). Используется для шаблонов, фоновых картинок, или если юзер хочет добавить текст потом сам.
- Pydantic `ApiCoverJobCreate` больше не отвергает пустые values (валидация ключей languages⇄titles_per_lang остаётся)
- UI убрал alert «Заполни названия» — пустые поля просто отправляются как ""
- В UI placeholder подсказывает: `"Название на X (пусто = без текста)"`
- Bugfix: `base_title` для scene generation теперь корректно fallback'ится на первый непустой title → extra_prompt → "untitled scene" (раньше падал «title must not be empty» если все пустые)

**v1.0.6 (2026-05-22)** — max_threads limit raised 16→20, parallel logging
- UI selector max 16→20, Pydantic le=10→20, /jobs/cover clamp 8→20
- Логи job содержат `[parallel] N workers for M items (max_threads=X)`

**v1.0.5 (2026-05-22)** — title-mode intra-parallel fix
- `_process_batch_title_mode` теперь использует `ThreadPoolExecutor(max_workers=max_threads)` — раньше был sync for-loop, max_threads игнорировался



**v1.0.4 (2026-05-22)** — Title mode
- Добавлен `mode=title`: генерация обложки из названия (`titles_per_lang`) + опциональных деталей (`extra_prompt`) **без YouTube URL**
- Сцена генерируется GPT один раз на job из `base_title + extra_prompt` (на английском, для consistency с Fast Gen)
- Текст обложки на каждом языке задаётся юзером в `titles_per_lang[lang]` — никакой LLM-адаптации
- В UI добавлен переключатель «📺 От YouTube / ✏️ От названия»
- Backward-compat: `mode=url` — дефолт, существующие интеграции не меняются

**v1.0.3 (2026-05-21, историческая версия)** — Gemini SDK removed, ENV fallback, Preset CRUD
- **Удалён** `gemini_api_key` из API: Gemini SDK (`google.generativeai`) больше не используется, анализ всегда через OpenAI
- **`openai_api_key` и `fast_gen_api_key` теперь опциональны**: если не переданы в body — сервер берёт `COVER_OPENAI_KEY` / `COVER_FAST_GEN_KEY` из своих переменных окружения
- **Новые endpoints**: `GET /api/v1/presets/{name}`, `POST /api/v1/presets`, `PUT /api/v1/presets/{name}`, `DELETE /api/v1/presets/{name}` — программное управление пресетами
- DELETE пресета → 409 если есть активный (pending/running) job с этим preset_name
- Пресеты со старым `analysis_provider=google` авто-мигрируются в `openai` при загрузке (silent)
- В UI убраны inputs «Gemini API ключ» и опция «Google (Gemini)» в провайдере анализа. **Опция «Gemini — Imagen» в моделях генерации сохранена** — это Fast Gen Imagen, использует `fast_gen_api_key`, не SDK
- `generate_image_gemini_fastgen` (Fast Gen Imagen) **не затронут**
- Legacy: CLI `--gemini-key` сохранён с DEPRECATION warning (печатает в stderr и игнорирует значение)

**v1.0.2 (2026-05-21)** — Language resolver + doc accuracy
- POST `/jobs` теперь принимает английские названия языков (`Russian`, `English`) — раньше падало 422
- `progress_by_language` ключи — ISO-коды (`ru`, `en`), как и обещано в этом документе (раньше были русские названия)
- Добавлены поля `code`, `display`, `files` в `progress_by_language[code]`
- Создан пресет **`Default RU`** (Русский + Английский) — для дефолтных интеграций
- Исправлен формат ответа `GET /presets` в доке: это **dict** `{name: PresetInfo}`, не массив
- Убрано неверное описание "особых случаев коротких кодов" (`po`, ambiguous `ar`/`es`)

**v1.0.1 (2026-05-20)** — Hotfix lang_code parsing
- Исправлен `GET /jobs/{id}/files/{lang_code}` (возвращал 404 для готовых файлов)
- Исправлен `progress_by_language` (показывал pending для готовых языков)

**v1.0.0 (2026-05-20)** — Initial release
- 7 endpoints под `/api/v1/*`
- Auth через API keys (cvr_ prefix)
- UI для управления ключами

# Cover Studio

## Назначение

Cover Studio превращает видео, название или подготовленное семя в набор обложек по языкам.
Она не просто вызывает image API: сначала получает контекст, анализирует reference, строит
prompt, адаптирует текст, генерирует варианты, проверяет результат и сохраняет files.

## Состав

| Файл | Ответственность |
|---|---|
| `cover_server.py` | FastAPI, UI, auth, jobs, favorites и orchestration |
| `cover_core.py` | анализ, prompts, provider adapters и batch processing |
| `job_queue_cover.py` | SQLite queue, fair scheduling, cancel/retry/resume |
| `provider_policy.py` | UI provider → credential/capacity group |
| `api_cover.py` | versioned REST API |
| `api_cover_auth.py` | API keys |
| `api_cover_helpers.py` | server credential pools/leases и admission |
| `usage_cover.py` | usage/budget accounting |

Фактический production snapshot и локальный репозиторий расходятся. В частности, production
имеет queue pulse/events, а локальная ветка — более свежую санитаризацию логов, которой нет на
сервере. В архиве `source/cover` — production-вариант; локальные улучшения перечислены как
известный разрыв, а не подмешаны в рабочий код.

## Режимы

### URL

Пользователь передаёт YouTube URLs. Сервис извлекает video ID, получает title через oEmbed,
скачивает thumbnail, анализирует composition/style/text и создаёт локализованные варианты.

### Title

Пользователь передаёт точное название для каждого языка и optional extra prompt. URLs в этом
режиме запрещены, а keys `titles_per_lang` должны точно совпасть с выбранными languages.

### Seed

Пользователь передаёт готовое семя для каждого языка и число вариантов. Keys семян обязаны
точно совпасть с languages, иначе расчёт quota и фактическая генерация разойдутся.

Seed поддержан UI route `/jobs/cover`, но отсутствует в партнёрском `/api/v1/jobs`. Это не
следует случайно «исправлять» при переносе: сначала нужно решить, должен ли seed стать частью
публичного API, и только затем версионировать контракт.

## Preset

Preset хранит provider/model, reference image, aspect ratio, prompt templates и параметры
анализа. User-provided base URL проходит SSRF guard: arbitrary endpoint не должен получать
server credential.

В новой системе preset должен быть versioned. Release должен ссылаться на immutable preset
version, а не на mutable name.

## Provider groups

Пользовательское название модели не равно реальному лимиту аккаунта. `provider_policy.py`
сопоставляет UI choice:

- gateway/fastgen group;
- direct xAI group;
- OpenAI image group;
- Veo-compatible group.

Scheduler должен ограничивать реальный credential/capacity group. Некоторые дорогие providers
доступны только администратору; server обязан проверять это независимо от скрытия UI option.

## Выполнение job

Перед enqueue service проверяет mode, размеры batch, languages, preset и наличие server keys.
Worker получает credential lease, вызывает `process_batch`, обновляет progress и сразу
сохраняет готовые files.

Job считается полным только если все ожидаемые variants прошли проверку. Fallback-обложки без
текста записываются отдельно и дают `completed_partial`, а не `completed`.

## Retry и resume

- Retry сбрасывает job и создаёт новую попытку по правилам queue.
- Resume использует existing output list и повторяет только missing/fallback artifacts.
- Cancel сохраняется в DB и проверяется во время provider waits.
- `run_token` защищает от старого writer после restart/retry.

Новый platform stage должен представлять каждый ожидаемый cover variant отдельным artifact
slot, чтобы partial completion не кодировался только двумя JSON-списками внутри одной строки.

## Favorites и retention

Обычные результаты временные и чистятся по retention. Favorites копируют выбранные файлы в
отдельное пользовательское хранилище с index. Cleanup обязан учитывать references, сохранённые
в presets, иначе можно удалить долгоживущий source image.

В целевой системе object storage lifecycle и database references должны заменить ручной scan
папок.

## REST API

OpenAPI snapshot: `contracts/openapi/cover.openapi.json`. Основные ресурсы:

- upload reference file;
- list presets/languages;
- create/list/get/cancel/retry/resume job;
- list/download files и archive;
- favorites;
- usage;
- API keys.

Client не должен передавать server API keys. Secret-like fields в request schema отклоняются.

Publication plane VSGen вызывает Cover по donor URL: обложка строится по превью исходного
ролика, а не по уже переписанному сценарию. Сейчас соседство дополнительно завязано на
совпадающий `username` и прямое чтение Cover SQLite/preset-файлов. Целевая интеграция должна
заменить это versioned API и стабильными tenant IDs.

## Проверка результата

Текущий core проверяет наличие и формат files, но image quality остаётся partly heuristic.
Новая система должна разделить:

- техническую валидность: декодируется, размер, aspect ratio, SHA;
- contract completeness: все языки/variants;
- policy: safety, provider fallback;
- creative selection: автоматический score или выбор человека.

Creative rejection не должна превращать технически валидный artifact в потерянную job.

Текущий publication adapter проверяет скачанную Cover только по размеру не менее 5000 bytes,
а затем пишет прямо в `cover.jpg`; upload gate проверяет в основном существование файла.
Crash или partial body может стать входом загрузки. Целевой adapter пишет temp, декодирует
изображение, проверяет dimensions/aspect/SHA, fsync и только затем атомарно заменяет current.

## Основные риски

1. `cover_core.py` и `cover_server.py` объединяют слишком много обязанностей.
2. Три режима и несколько providers имеют неодинаковую семантику input/output.
3. Global account capacity делится с другими Studio, но локальный scheduler видит только Cover.
4. Mutable presets и filesystem refs усложняют воспроизводимость.
5. Partial/fallback состояние хранится внутри JSON и легко неверно спроецировать в UI.
6. Local repository отличается от production snapshot.
7. Партнёрский batch может оказаться намного дороже UI batch; нужны единые cost/admission limits.
8. Удаление пользователя и отзыв API keys должны быть одной транзакционной политикой.

## Что переносить как контракт

- точная validation mode-specific inputs;
- immutable preset version;
- credential/capacity group mapping;
- server-side provider authorization;
- per-user fair queue;
- persistent cancel/run token;
- resume только missing variants;
- completed vs completed_partial;
- tenant/path isolation;
- separate temporary results and durable favorites.

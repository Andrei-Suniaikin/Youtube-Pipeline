# Rewrite Studio

## Назначение

Rewrite Studio создаёт новый сценарий по теме или переписывает загруженный текст на один или
несколько языков. Пользователь выбирает preset, модель, языки и параметры структуры. На выходе
получает отдельные TXT-файлы и meta/progress по job.

## Состав

| Файл | Ответственность |
|---|---|
| `server.py` | FastAPI, UI, admission, orchestration и endpoints |
| `rewrite_core.py` | prompts, chunking, provider calls, postprocess и checkpoints |
| `job_queue.py` | SQLite lifecycle, fair scheduling, cancel/retry/recovery |
| `api.py` | tenant-safe `/api/v1` |
| `api_auth.py` | API keys и dependencies |
| `auth.py` | login/password sessions/accounts |
| `log_public.py` | default-deny фильтр публичного лога |
| `subs_fetch.py` | YouTube transcript → text через proxy-aware client |
| `gen_v7.py`, `gen_output_cleaner.py` | новый generation flow и нормализация результата |
| `gen_editor.py`, `gen_judge.py`, `qa_findings.py` | редактура, оценка и QA generation |

## Два типа job

### Generation

Вход — тема, target language, target length и generation preset. Текст строится несколькими
LLM turns/chunks и возвращает структурированные fields: preview, title, script и progress.
Для subscription providers длинная генерация может идти в одной conversation session.

### Rewrite

Вход — один или несколько файлов, languages и rewrite settings. Для каждого file-language
создаётся независимая задача/подзадача. Текст делится на chunks, результаты сохраняются по
частям, затем собираются и проверяются.

Режим `preserve_paragraphs` требует 1:1 структуру абзацев и избегает postprocess, который
может эту структуру сломать.

## Provider selection

Поддерживаются OpenAI, Claude и Codex. Пользователь выбирает разрешённую модель, но не
конкретный server credential. Admission проверяет allowlist, доступ пользователя и наличие
server/BYOK capacity.

В текущем production несколько OpenAI/Anthropic keys выбираются round-robin. Если выбрана
допустимая OpenAI-модель, но OpenAI credential отсутствует, default-on fallback может незаметно
перевести задачу на Codex. В новой системе такой fallback должен быть явной policy, записанной
в attempt и показанной пользователю до расхода средств.

Server-owned credentials не должны попадать в payload, log или API response. BYOK хранится
на стороне server account и выбирается политикой, а не произвольным client field.

## Очередь

`JobQueue` хранит job JSON и first-class columns: status, user, provider, cancel flag,
run token и timestamps. Claim выполняется транзакционно.

`JobWorker`:

- ограничивает общую concurrency;
- применяет provider group limits;
- выбирает пользователей round-robin;
- соблюдает FIFO внутри пользователя;
- не доверяет legacy client `parallel`;
- восстанавливает running/resetting jobs после restart.

`run_token` меняется на каждом claim. Progress/final write принимается только от текущего run.
Это обязательный контракт новой платформы.

## Checkpoints

`rewrite_core.py` хранит состояние chunks по user, job, source file и language. Готовые части
не должны оплачиваться повторно. Итоговый статус различает complete, partial и failed.

Новый orchestration layer должен вынести checkpoint schema из process-local singleton/JSON
в явные tables или object store records, но сохранить гранулярность chunk attempts.

## Cancel, retry и restart

- Pending cancel сразу становится terminal cancelled.
- Running cancel ставит persistent flag; worker проверяет его в waits/provider loops.
- Stale worker с прежним token не может завершить новую попытку.
- Full retry сначала фиксирует resetting/cleanup manifest, потом удаляет outputs.
- Interrupted job после restart возвращается в pending либо остаётся cancelled.

Delete и retry — разные операции. Удаление UI-записи не должно тихо стирать audit trail.

## Логи

Публичный job log проходит allowlist и содержит только понятный progress и безопасные error
codes. Полный технический лог хранится отдельно и не отдаётся tenant API.

Новый log pipeline должен сохранить разделение:

- user events без prompts, raw model output и secrets;
- operator structured logs с trace/release/job/attempt IDs;
- immutable audit для действий администратора.

UI обещает хранение результата 30 дней, тогда как фактический default cleanup равен 7 дням.
Целевая retention policy должна иметь один источник истины и быть проверяемой тестом.

## YouTube subtitles

`subs_fetch.py` скачивает transcript без yt-dlp. Облачный IP часто блокируется YouTube,
поэтому production использует proxy/tunnel. DNS resolution для SOCKS должен выполняться через
proxy. При server block вся пачка останавливается, чтобы не умножать бессмысленные запросы.

## REST API

OpenAPI snapshot находится в `contracts/openapi/rewrite.openapi.json`. Основные ресурсы:

- upload input file;
- create/list/get/cancel/retry job;
- download language result;
- presets/models/languages;
- API-key management;
- transcript fetch/download.

Новая API должна сохранить versioning и tenant isolation. UI может использовать ту же API,
но session auth и machine API keys должны иметь разные scopes.

## Основные риски

1. `rewrite_core.py` почти десять тысяч строк и смешивает provider adapters, prompts,
   checkpoints, quality passes и persistence.
2. Часть credential state исторически могла храниться в users DB; миграция должна scrub-ить
   legacy fields только после согласованного backup/preflight.
3. Несколько Studio делят одни subscription accounts, но Rewrite scheduler видит только свои jobs.
4. Conversation retries нельзя трактовать как обычный stateless HTTP retry.
5. Local repository и production snapshot различаются; production source в архиве приоритетнее.
6. Generation/QA реализованы динамически импортируемыми модулями; fail-open import меняет
   возможности приложения без ошибки старта.

## Что переносить как контракт

- provider/model admission;
- per-user fair queue и provider capacity;
- run token/CAS writes;
- persistent cancel и restart recovery;
- chunk-level resume;
- exact paragraph/language validation;
- public/private log separation;
- server-owned secret boundary;
- tenant-safe file ownership и path traversal guards.

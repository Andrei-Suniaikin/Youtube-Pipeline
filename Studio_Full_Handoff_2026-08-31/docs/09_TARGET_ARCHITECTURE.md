# Предлагаемая целевая архитектура

Это не требование использовать конкретный framework. Это минимальная форма системы, в которой
restart, timeout и частичный сбой являются штатными событиями, а не ручной катастрофой.

## Основной выбор

На первом этапе разумнее один модульный control plane, а не россыпь микросервисов:

- один versioned API и одна модель `release`;
- PostgreSQL как источник истины для состояний, leases, receipts и outbox;
- S3-совместимое object storage для текстов и media;
- отдельные stateless workers по capability;
- отдельные edge-agents для рендера и браузерной загрузки;
- web UI и Desktop как клиенты одного API.

Границы модулей должны быть строгими, но их не обязательно сразу разносить по отдельным
deployments. Это уменьшает распределённые транзакции. Broker можно добавить, когда Postgres
claim/outbox перестанет выдерживать измеренную нагрузку; начинать с двух систем очередей без
необходимости не нужно.

## Слои

### 1. Control plane

Содержит auth, tenants, каналы, content plan, releases, immutable schedule revisions, stage
state machine, presets, quotas, provider capacity, audit и API. Он не рендерит видео и не
управляет браузером внутри HTTP request.

### 2. Orchestrator и recovery controller

Orchestrator создаёт только готовые к выполнению stages. Worker claim-ит stage с fencing token
и lease. Recovery controller разбирает expired leases и receipts: безопасную работу requeue,
неоднозначную платную/браузерную операцию переводит в reconcile, terminal problem изолирует.

### 3. Capability workers

Минимальные capability:

- source/transcript;
- generation/rewrite;
- TTS create, poll, download/validate;
- scene planning;
- image create и image reconcile;
- generated video create и reconcile;
- cover generation;
- render;
- chapters/metadata;
- upload/reconcile/publication verification.

Worker получает immutable input manifest и пишет artifacts/receipts через API. Он не сканирует
«какую-нибудь подходящую папку» и не меняет соседнюю stage напрямую.

### 4. Provider gateway и capacity manager

Каждый adapter реализует одинаковую форму: validate, create, poll, cancel-if-supported,
download и classify_error. Credential хранится в secret store, а job видит только opaque ID.
Capacity считается по реальному общему аккаунту, даже если его используют Rewrite, Cover и
VideoStudio под разными названиями.

### 5. Artifact store

DB хранит ownership, SHA-256, media type, producer attempt, input fingerprint и validation.
Object storage хранит bytes. Публикация artifact атомарна; незавершённые uploads живут в
staging namespace и чистятся отдельно.

### 6. Edge agents

Render и upload agents регистрируют host, version, capabilities и heartbeat. Control plane
выдаёт им scoped claim и ссылки только на конкретные artifacts. Upload agent дополнительно
получает глобальную lease AdsPower profile и после `upload_started` работает только в режиме
reconcile/finalize.

### 7. Интерфейсы

Web UI показывает один timeline release. Desktop может остаться Windows-клиентом для локальной
работы, но не содержит вторую копию бизнес-оркестрации. Если нужен offline mode, он создаёт
тот же manifest и позднее синхронизирует artifacts через явный protocol.

## Транзакционная граница

В одной DB transaction меняются domain state и outbox command. Отдельный dispatcher доставляет
команду worker-у. Результат принимается compare-and-set только от текущего attempt/token.
Таким образом crash до commit не создаёт работу, а crash после commit не теряет команду.

Внешняя операция не может войти в DB transaction, поэтому для неё обязательны intent,
idempotency key, external ID и receipt. Неопределённость — отдельное состояние, а не повод
повторить create.

## Один release по шагам

```text
plan item frozen
  → active immutable schedule revision
  → source artifact
  → script artifact
  → narration artifacts
  → scene manifest
  → image/video artifacts
  → render manifest → validated MP4
  → validated cover
  → upload intent → uploaded video ID
  → verified scheduled
  → verified public
```

Каждая стрелка оставляет событие, attempt и проверяемый artifact/receipt. Несвязанный failure
одного release не останавливает остальные каналы.

## Что оставить адаптерами на переходный период

- текущий Rewrite `/api/v1`;
- текущий Cover `/api/v1`;
- существующие TTS/image/video workers;
- текущий FFmpeg render worker за manifest adapter;
- upload worker/YouTube DOM adapter за новым claim/receipt protocol.

Так можно сначала заменить управление и доказать восстановление, не переписывая одновременно
все provider-specific детали. Затем adapters заменяются по одному, сохраняя contract tests.

## Что не переносить

- inline HTML и orchestration в одном многотысячном файле;
- прямое чтение соседних SQLite и папок;
- связь пользователей по `username`;
- mutable preset без версии;
- status из exit code;
- raw secrets в job JSON/admin state;
- очереди только в памяти;
- blind retries платных create или YouTube upload;
- большой production cutover без per-stage feature flag и rollback.

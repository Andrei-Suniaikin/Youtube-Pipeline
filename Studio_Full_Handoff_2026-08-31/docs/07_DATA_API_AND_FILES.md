# Данные, API и файлы

## Текущие базы

В production используются пять основных SQLite-файлов:

| Product | DB | Что хранит |
|---|---|---|
| VSGen | `vsgen.db` | users, projects, execution queue, admin, channels/plan/releases |
| Rewrite | `sessions.db` | accounts, sessions, API keys, user/provider settings |
| Rewrite | `job_queue.db` | jobs, progress, chunk/task state и scheduling columns |
| Cover | `cover_sessions.db` | accounts, sessions, API keys |
| Cover | `cover_job_queue.db` | jobs, provider/user scheduling, cancel/run token |

В архиве только DDL: `contracts/schema/*.sql`. Ни одной production-строки нет.

SQLite настроен с WAL и busy timeout. Для текущего одиночного процесса это помогает, но не
заменяет server-grade coordination, миграции и backup discipline при росте числа agents.

## Основные сущности текущего VSGen

### Проекты и jobs

- `projects`: tenant, project directory, status и settings JSON;
- `gen_jobs`: user, type, model, source/settings/result/error;
- `queue_tasks`: kind/provider/lane/status/claim/progress и retry fields;
- `job_logs`, `job_files`: progress и outputs;
- `presets`, `voice_lib`: пользовательские настройки.

### Публикация

- `pub_channels`: точный канал, profile/host, timezone, schedule и defaults;
- `pub_plan_items`: source/donor, slot/order, channel и status;
- `pub_releases`: immutable-ish execution projection;
- `pub_release_events`, `pub_release_log`: evidence/audit;
- `pub_upload_jobs`: durable upload claims;
- `pub_release_jobs`: связь release с jobs соседних стадий.

При миграции эти записи нужно не копировать в один JSON, а разложить на versioned domain tables.

## Рекомендуемая целевая модель данных

Минимальный набор в Postgres:

- `tenants`, `users`, `roles`, `api_keys`;
- `channels`, `channel_schedules`, `upload_profiles`, `agents`;
- `presets`, `preset_versions`;
- `plan_items`, `plan_item_revisions`;
- `releases`, `schedule_revisions` (с active revision pointer и полной историей переносов);
- `stage_instances`, `stage_attempts`, `leases`;
- `artifacts`, `artifact_validations`, `artifact_links`;
- `external_operations`, `receipts`;
- `provider_credentials`, `provider_capacity`, `capacity_leases`;
- `events`, `outbox`, `audit_log`;
- `human_tasks` для auth gate/manual recovery.

Большие texts, raw model outputs и media лежат в object storage, а DB содержит ссылки, hashes,
version и ownership. Секреты хранятся отдельно от domain DB или зашифрованы envelope encryption.

## API snapshots

`contracts/openapi/` содержит live OpenAPI трёх сайтов на дату архива:

- `vsgen.openapi.json`;
- `rewrite.openapi.json`;
- `cover.openapi.json`.

Это фактический route/schema inventory, но не обещание сохранить все legacy paths. Новая API
должна иметь version prefix, scopes и idempotency headers.

### Рекомендуемые platform resources

```text
/api/v1/channels
/api/v1/plan-items
/api/v1/releases
/api/v1/releases/{id}/schedule-revisions
/api/v1/releases/{id}/stages
/api/v1/jobs/{id}
/api/v1/artifacts/{id}
/api/v1/human-tasks
/api/v1/agents/claim
/api/v1/agents/{id}/heartbeat
/api/v1/provider-capacity
/api/v1/events
```

Product-specific routes могут жить под `/rewrite`, `/cover`, `/video`, но используют те же
auth, jobs, artifacts и event contracts.

## API правила

1. Любая create-команда принимает `Idempotency-Key`.
2. Tenant определяется auth context, а не body field.
3. Server credentials никогда не принимаются в обычном job payload.
4. File upload возвращает content-addressed artifact ID.
5. Download разрешает только owned artifact и безопасное имя.
6. Long operations возвращают job/stage ID, а не держат HTTP request.
7. List routes имеют pagination и stable ordering.
8. Cancel, retry, resume и reconcile — разные explicit commands.
9. Error response содержит public code, message и safe retry guidance.
10. Admin mutations пишут audit before/after без secret values.

## Текущие файловые контракты

### Rewrite

- uploads принадлежат user/job;
- outputs делятся по языкам;
- raw technical logs лежат отдельно от public job log;
- chunk/checkpoint state связан с user, job, basename и language.

### Cover

- per-user upload references;
- temporary job outputs;
- fallback output list;
- per-user presets/prompts;
- favorites как отдельное durable хранилище.

### VideoStudio

- нумерованные TTS MP3;
- `scenes.json` + `scenes.meta.json`;
- `NNN.jpg` и `NNN_video.mp4`;
- reference manifests/revisions/receipts;
- render temp и MP4;
- upload target/manifest/receipt.

Подробные примеры release, attempt, artifact, render manifest и upload receipt находятся в
`contracts/examples/`.

## Правила путей

- Никогда не доверять client relative path без canonical resolution.
- Artifact ID не должен быть обычным абсолютным path.
- Worker получает presigned/read-scoped location на конкретные inputs.
- Output публикуется только в назначенный staging prefix.
- Filename — presentation metadata; identity задаёт artifact ID и SHA.
- Cleanup работает по DB references и retention policy, а не только по mtime scan.

## Config

Нужно разделить три вида конфигурации:

1. **Deploy config**: DB URL, storage, endpoints, timeouts, feature flags.
2. **Tenant config**: presets, languages, allowed models, quotas.
3. **Secrets**: API keys, OAuth, tokens, passwords, private browser coordination credentials.

Secrets не должны попадать в logs, manifests, queue payloads или UI diagnostics. В архиве есть
только `config/env.example` с placeholders.

## Backups и миграции

- миграции versioned и идемпотентные;
- перед destructive/scrub миграцией consistent backup и restore rehearsal;
- файлы и DB меняются через двухфазный manifest/outbox, а не несогласованно;
- rollback учитывает новые statuses/kinds; старый code не запускается против непонятной схемы;
- backup считается готовым только после проверенного restore в изолированную среду.

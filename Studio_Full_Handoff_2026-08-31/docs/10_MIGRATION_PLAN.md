# План переписывания без остановки производства

Переписывание нужно вести как замену стадий работающей линии. Big-bang версия опасна: даже
если happy path работает, в ней сразу неизвестны provider limits, recovery, Windows build,
render-ноды и YouTube edge cases.

## Развилка объёма

**Безопасный минимум:** новый control plane, единый release/state contract, Postgres,
artifact store и recovery; текущие Rewrite/Cover/workers подключены адаптерами. Это даёт
примерно 60–75% практической устойчивости с меньшим риском.

**Полный фикс:** минимум плюс переписанные provider adapters, UI, Desktop thin client,
render/upload agents, единая capacity/security/observability и отказ от старых DB. Это даёт
целевой результат, но только поэтапно после доказанного минимума.

## Фаза 0. Зафиксировать baseline

До кода:

1. Получить актуальные снимки Windows build и обеих edge-нод.
2. Зафиксировать production source SHA, schemas, route inventory и config names.
3. На синтетическом tenant записать expected artifacts/receipts для одного релиза.
4. Определить SLO, retention, квоты, понятие `scheduled` и `published`.
5. Создать threat model и список внешних платных границ.

Выход: подписанный baseline manifest и список известных расхождений. Production не меняется.

## Фаза 1. Каркас платформы

Создать один репозиторий, reproducible dev environment, migrations, Postgres schema, object
storage, auth/tenant isolation, release API, outbox, leases, events и synthetic workers.

Обязательный canary: полностью синтетический release проходит все stages; process принудительно
убивается на каждой границе и после restart завершается без дублей.

## Фаза 2. Legacy adapters

Подключить Rewrite и Cover как remote adapters, а TTS/image/render как worker adapters.
Каждый adapter обязан преобразовать legacy status/files в новый artifact/receipt contract и
иметь timeout/reconcile policy.

Перед любым автоматическим create в Rewrite/Cover обязателен отдельный pre-gate. Их текущие
API не принимают общий idempotency key, поэтому control plane не может закрыть crash-window
«remote job создана, но её ID ещё не сохранён». Нужно сначала аддитивно добавить в legacy API
`Idempotency-Key`, UNIQUE/replay и lookup по ключу. Если это невозможно, create-adapter
остаётся unsafe/manual: после неоднозначного timeout он не повторяет POST, а создаёт human
reconcile task.

Обязательный failure test: убить platform сразу после создания legacy remote job, но до записи
`remote_job_id`; после restart повтор команды обязан найти ту же job и не создать вторую.

Сначала только shadow: новая платформа читает/сравнивает, но не заказывает повторную платную
работу. Потом один allowlisted canary tenant с cost ceiling.

## Фаза 3. Сквозной реальный canary без публикации

Один короткий источник проходит rewrite → TTS → scenes → images → render → cover. MP4 и cover
проверяются, но YouTube upload выключен. Результаты сравниваются с текущим production по
полноте, длительности, hashes/metadata и ручной creative QA.

Gate: повтор того же release не создаёт новых provider operations.

## Фаза 4. Upload agent

Перенести cross-host profile lease, exact UC preflight, durable upload boundary, receipts и
reconcile. Сначала private/unlisted test channel. Провести crash tests до и после file input,
после video ID и после schedule save.

Gate: ни один тест не создаёт второй YouTube upload для одного release; `scheduled` доказан
machine state, `published` проверяется отдельно после срока.

## Фаза 5. Стадии заменяются по одной

Рекомендуемый порядок: Rewrite → Cover → TTS → scene/image/video → render → upload UI. У каждой
стадии:

- adapter contract tests;
- shadow comparison;
- tenant/channel allowlist;
- cost and concurrency ceiling;
- old/new feature flag;
- drain и rollback procedure;
- наблюдение хотя бы одного полного производственного цикла.

## Фаза 6. UI и Desktop

Web UI переходит на release API. Desktop становится thin/edge client или отдельно согласованным
offline client с теми же manifests. Windows build получает pinned toolchain, clean-machine CI,
SBOM и checksum.

## Фаза 7. Миграция данных

Сначала read-only import mapping, затем shadow sync, потом dual-read с reconciliation report.
Переход записи делается по tenant, не по всей системе. Legacy DB остаётся read-only на период
аудита. Media переносится content-addressed и проверяется SHA.

## Rollback

Rollback — заранее протестированное действие:

- новые claims выключаются;
- running attempts drain/fence;
- stages с external side effect проходят reconcile;
- маршрутизация возвращается adapter-у;
- schema остаётся backward-readable либо deploy блокируется;
- audit показывает, какие releases затронуты.

Нельзя откатывать код, если он не понимает уже записанные statuses. Нельзя удалять новые данные
ради отката.

## Отчёт после каждой фазы

Разработчик передаёт: commit/image hash, migrations, список проверок, canary release ID, расходы,
failure injection результаты, известные ограничения и точную команду rollback. Формулировка
«проверено вручную» без evidence недостаточна.

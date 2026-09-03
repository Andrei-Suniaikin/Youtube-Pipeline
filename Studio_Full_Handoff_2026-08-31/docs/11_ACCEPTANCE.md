# Критерии приёмки новой системы

Эта матрица нужна владельцу и разработчику. Она проверяет не красоту кода, а способность линии
не терять работу, деньги и каналы.

## Сквозная функциональность

- Один plan item создаёт ровно один immutable release для exact channel UC.
- Rewrite/generation сохраняет ожидаемые языки и структуру; частичный chunk не публикует итог.
- Все TTS/image/video/cover slots известны заранее, а partial не называется success.
- Render использует только manifest, создаёт декодируемый MP4 с narration и ожидаемой длиной.
- Upload использует точный channel/profile и не дублируется.
- `scheduled` и `published` имеют разные evidence и timestamps.
- Operator видит timeline, artifacts, attempts, receipts и безопасное действие восстановления.

## Restart и chaos

Для каждой stage process убивается как минимум:

1. до claim commit;
2. после claim, до side effect;
3. во время provider call;
4. после внешнего success, до local commit;
5. во время download/write;
6. после artifact validation, до stage completion.

Ожидание: old writer fenced; deterministic work безопасно resume; paid create не дублируется;
ambiguous операция становится reconcile; валидный artifact не теряется.

Отдельно для YouTube проверяются crash до file input, сразу после него, после video ID и после
сохранения расписания. Во всех случаях один release создаёт не более одного upload.

## Идемпотентность

- Повтор HTTP create с тем же `Idempotency-Key` возвращает тот же resource.
- Повтор worker result с тем же token не создаёт второй artifact/event.
- Result от старого token отклоняется.
- Повтор completed release не вызывает внешних providers.
- Изменение реального input/preset создаёт новый fingerprint и не переиспользует старый output.

## Очередь и fairness

- Tenants выбираются round-robin, FIFO сохраняется внутри tenant.
- Один большой batch не блокирует маленькие jobs навсегда.
- Реальный shared credential имеет общий capacity across products.
- Pause provider/tenant/stage не останавливает независимые lanes.
- `Retry-After`/subscription reset сохраняется durable; restart не превращает его в tight loop.

## Данные и artifacts

- Ни один status `succeeded` не существует без всех обязательных validations.
- Artifact SHA после upload/download совпадает.
- Temp/partial файл не становится current.
- Cleanup не удаляет referenced artifact/favorite/preset source.
- Backup Postgres и object storage восстановлен в изолированной среде, связи и hashes проверены.
- Migration можно повторить или безопасно определить как уже выполненную.

## Безопасность

- Tenant A не может list/get/download/cancel ресурс tenant B.
- API keys хешируются, scoped, revocable; удаление пользователя отзывает их.
- Server/provider secrets не появляются в API, browser storage, logs, traces, manifests и dumps.
- SSRF, path traversal, unsafe archive names и oversized batches отклоняются.
- Login/API rate limits работают между рестартами и несколькими replicas.
- User error не содержит traceback, absolute paths, prompts или raw provider response.
- Edge agent имеет минимальный scoped token, rotation и host identity.

## Deploy и воспроизводимость

- Clean machine строит тот же dependency lock и immutable image/binary.
- Каждая worker result содержит build/capability version.
- Schema compatibility проверяется до запуска worker.
- Canary и rollback автоматизированы; старые workers drain или fenced.
- Windows EXE проходит clean Windows smoke: запуск, создание проекта, одна бесплатная stage,
  restart и чтение существующего проекта.

## Наблюдаемость

- Есть liveness, readiness, dependency, queue и business health.
- Метрики показывают oldest wait, leases, retries, partial, reconcile, cost и публикационные gaps.
- Alert имеет runbook и не зависит от просмотра одной HTML-страницы.
- Один release ищется по ID через UI, structured logs и audit без ручного grep нескольких машин.

## Минимальный приёмочный набор сценариев

| Сценарий | Ожидаемый результат |
|---|---|
| Короткий end-to-end без YouTube | validated script/audio/images/render/cover |
| Два tenants, один provider | fairness и tenant isolation |
| 59/60 images | partial, resume только slot 60 |
| Timeout после paid create | poll/reconcile, ноль повторных заказов |
| Crash после legacy create до сохранения remote ID | replay/lookup той же job, ноль дублей |
| Старый MP3 лежит рядом | не попадает в render manifest |
| Render crash с partial MP4 | current artifact не меняется |
| Cover write оборван после 5 KB | partial quarantined, current cover не меняется |
| Upload crash после file input | reconcile существующего video, не re-upload |
| Release не успел к slot | новый immutable `schedule_revision_id`, тот же `release_id`; старые revision/attempt сохранены |
| Wrong UC/redirect/login gate | fail closed + human task |
| Schedule наступило | отдельно verified PUBLIC либо понятный failure |
| Restore backup | тот же release graph и artifact hashes |

## Definition of Done

Фаза считается готовой только когда код запущен, результаты сценариев приложены, failure
injection пройден, cost/side effects перечислены, rollback выполнен хотя бы на staging и нет
необъяснённых расхождений. Одного code review или зелёного unit test недостаточно.
